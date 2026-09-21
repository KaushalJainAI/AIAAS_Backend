"""
Browser Pro (P2): sessions, vault logins, downloads, the submit gate.

Pinned: one user per profile and one domain per profile (a session never
crosses either); idle and over-age sessions expire; a secret typed into a
page never reaches the transcript; an OTP/CAPTCHA comes back as a question
for a person, never a guess; a call moving toward submit says so.
"""
from __future__ import annotations

import json
import shutil
import tempfile
from datetime import timedelta
from unittest import mock

from asgiref.sync import async_to_sync
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone

from browsing import sessions
from browsing.engine import BrowserError, check_steps, looks_submitting
from browsing.models import BrowserSession
from chat.tools import execute_tool
from credentials.models import Credential, CredentialType
from inference import vfs

User = get_user_model()


def _page(**extra):
    page = {'url': 'https://portal.example.com/invoices', 'title': 'Invoices',
            'text': 'Invoice 1: total 120', 'truncated': False, 'links': [],
            'steps': [{'action': 'click', 'ok': True}], 'screenshot': None}
    page.update(extra)
    return page


class SessionTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user('surfer', 's@example.com', 'pw')
        self.other = User.objects.create_user('other', 'o@example.com', 'pw')

    def test_open_session_is_reused_while_fresh(self):
        first = sessions.ensure(self.user, 'portal.example.com')
        second = sessions.ensure(self.user, 'portal.example.com')
        self.assertEqual(first.id, second.id)

    def test_sessions_never_cross_users_or_domains(self):
        mine = sessions.ensure(self.user, 'portal.example.com')
        theirs = sessions.ensure(self.other, 'portal.example.com')
        other_site = sessions.ensure(self.user, 'bank.example.com')
        self.assertNotEqual(mine.id, theirs.id)
        self.assertNotEqual(mine.id, other_site.id)

    def test_the_sweep_expires_idle_and_over_age_sessions(self):
        fresh = sessions.ensure(self.user, 'portal.example.com')
        BrowserSession.objects.filter(id=fresh.id).update(
            last_used_at=timezone.now() - timedelta(hours=2))
        old = sessions.ensure(self.user, 'bank.example.com')
        BrowserSession.objects.filter(id=old.id).update(
            expires_at=timezone.now() - timedelta(minutes=1),
            last_used_at=timezone.now())
        counts = sessions.sweep()
        self.assertEqual(counts['idle_expired'], 1)
        self.assertEqual(counts['aged_expired'], 1)
        self.assertEqual(
            BrowserSession.objects.filter(status='open').count(), 0)


class StepValidationTests(TestCase):
    def test_new_verbs_validate(self):
        steps = check_steps([
            {'action': 'scroll', 'selector': '#list'},
            {'action': 'download', 'selector': '#pdf'},
            {'action': 'extract', 'selector': 'table', 'text': 'table'},
            {'action': 'fill_secret', 'selector': '#pw',
             'secret_ref': 'portal.password'},
        ])
        self.assertEqual(len(steps), 4)
        self.assertEqual(steps[-1]['secret_ref'], 'portal.password')

    def test_fill_secret_needs_a_reference(self):
        with self.assertRaises(BrowserError):
            check_steps([{'action': 'fill_secret', 'selector': '#pw'}])

    def test_upload_is_refused_with_a_reason(self):
        with self.assertRaises(BrowserError) as ctx:
            check_steps([{'action': 'upload', 'selector': '#f', 'text': '/x'}])
        self.assertIn('not available', str(ctx.exception))

    def test_extract_mode_is_closed(self):
        with self.assertRaises(BrowserError):
            check_steps([{'action': 'extract', 'selector': 't', 'text': 'sql'}])

    def test_submit_steps_are_recognised(self):
        self.assertTrue(looks_submitting(
            [{'action': 'click', 'selector': '#pay-now'}]))
        self.assertTrue(looks_submitting(
            [{'action': 'click', 'selector': 'button[type=submit]'}]))
        # Field *content* is not intent: typing "send the report" commits nothing.
        self.assertFalse(looks_submitting(
            [{'action': 'type', 'selector': '#body', 'text': 'please send the report'}]))
        self.assertFalse(looks_submitting(
            [{'action': 'click', 'selector': '#next'}]))


class BrowserActTests(TestCase):
    def setUp(self):
        self._media = tempfile.mkdtemp()
        self._override = override_settings(MEDIA_ROOT=self._media)
        self._override.enable()
        self.user = User.objects.create_user('owner', 'owner@example.com', 'pw')
        self.scope = vfs.chat_scope(self.user)
        cred_type, _ = CredentialType.objects.update_or_create(
            slug='portal-gst',
            defaults={'name': 'GST Portal', 'auth_method': 'custom',
                      'fields_schema': [{'name': 'password'}]},
        )
        self.cred = Credential(user=self.user, credential_type=cred_type, name='GST')
        self.cred.set_credential_data({'password': 's3cr3t-pw'})
        self.cred.save()

    def tearDown(self):
        self._override.disable()
        shutil.rmtree(self._media, ignore_errors=True)

    def call(self, args, **extra):
        ctx = {'user_id': self.user.id, 'file_scope': self.scope,
               'browser_domains': None, 'browser_logins': {'portal-gst'}, **extra}
        with mock.patch('browsing.engine.run', autospec=True) as run:
            run.side_effect = _fake_run
            out = json.loads(async_to_sync(execute_tool)('browser_act', args, ctx))
        return out, run

    def test_a_fill_secret_types_the_value_and_scrubs_it(self):
        out, run = self.call({
            'url': 'https://portal.example.com/login',
            'steps': [{'action': 'fill_secret', 'selector': '#pw',
                       'secret_ref': 'portal-gst.password'}],
        })
        sent = run.call_args[1]['steps']
        self.assertEqual(sent[0]['text'], 's3cr3t-pw')
        self.assertNotIn('secret_ref', sent[0])
        self.assertNotIn('s3cr3t-pw', json.dumps(out))

    def test_a_login_outside_the_allowlist_is_refused(self):
        out, run = self.call({
            'url': 'https://portal.example.com/login',
            'steps': [{'action': 'fill_secret', 'selector': '#pw',
                       'secret_ref': 'portal-gst.password'}],
        }, browser_logins=set())
        self.assertIn('not available', out['error'])
        run.assert_not_called()

    def test_an_otp_step_comes_back_as_a_question(self):
        out, run = self.call({
            'url': 'https://portal.example.com/login',
            'steps': [{'action': 'ask_user', 'prompt': 'Enter the OTP sent to …xx12'}],
        })
        self.assertIn('OTP', out['needs_user'])
        sent = run.call_args[1]['steps']
        self.assertTrue(all(s['action'] != 'ask_user' for s in sent))

    def test_a_session_is_reused_and_scoped_to_its_domain(self):
        out, _ = self.call({
            'url': 'https://portal.example.com/invoices',
            'steps': [{'action': 'click', 'selector': '#next'}],
            'session': 'new',
        })
        session_id = out['session']
        again, _ = self.call({
            'url': 'https://portal.example.com/invoices',
            'steps': [{'action': 'click', 'selector': '#next'}],
            'session': session_id,
        })
        self.assertEqual(again['session'], session_id)
        # Another domain is refused rather than crossed.
        crossed, _ = self.call({
            'url': 'https://bank.example.com/',
            'steps': [{'action': 'click', 'selector': '#x'}],
            'session': session_id,
        })
        self.assertIn('belongs to', crossed['error'])

    def test_a_download_lands_in_the_write_folder(self):
        import base64

        global _PDF_B64
        _PDF_B64 = base64.b64encode(b'%PDF-1.4 invoice').decode()
        with mock.patch('browsing.engine.run', autospec=True) as run:
            run.side_effect = _fake_download
            ctx = {'user_id': self.user.id, 'file_scope': self.scope,
                   'browser_domains': None, 'browser_logins': set()}
            out = json.loads(async_to_sync(execute_tool)('browser_act', {
                'url': 'https://portal.example.com/invoices',
                'steps': [{'action': 'download', 'selector': '#pdf'}],
            }, ctx))
        self.assertTrue(any('downloads/' in p for p in out['saved_files']))

    def test_a_submit_call_says_so(self):
        out, _ = self.call({
            'url': 'https://portal.example.com/pay',
            'steps': [{'action': 'click', 'selector': '#pay-now'}],
        })
        self.assertIn('submit_gate', out)


async def _fake_run(*args, **kwargs):
    return _page()


async def _fake_download(*args, **kwargs):
    import base64 as _b64

    return _page(download={'url': 'https://portal.example.com/i.pdf',
                           'mime': 'application/pdf', 'bytes': 14,
                           'data': _b64.b64decode(_PDF_B64)})


_PDF_B64 = None
