"""
Settings that change what the model is told.

Timezone, language, display name and bio were stored on the profile and read by
nothing on the model path. These tests pin the three places they now land —
chat's clock, the system prompt's "about the user" block, and an agent's sense
of "now" — plus the validation that makes those reads safe, and the one field
that must never be writable from Settings at all.
"""
from __future__ import annotations

from datetime import datetime, timezone as dt_timezone

from django.contrib.auth.models import User
from django.test import SimpleTestCase, TestCase
from rest_framework.test import APITestCase

from core.models import UserProfile
from core.preferences import (
    DEFAULTS, MAX_BIO_CHARS, Preferences, about_user, for_user, language_code,
    local_now, zone_is_valid,
)


class LanguageCodeTests(SimpleTestCase):
    def test_both_spellings_the_column_has_held_resolve(self):
        """The model default is `en`; the Settings page sent `English`."""
        for raw in ('en', 'EN', 'English', 'english', ' English '):
            with self.subTest(raw=raw):
                self.assertEqual(language_code(raw), 'en')
        self.assertEqual(language_code('Spanish'), 'es')

    def test_blank_is_the_default_and_nonsense_is_refused(self):
        self.assertEqual(language_code(''), 'en')
        self.assertIsNone(language_code('Klingon'))


class RenderingTests(SimpleTestCase):
    def test_defaults_render_no_block(self):
        """UTC and English tell the model nothing; no block beats boilerplate."""
        self.assertEqual(about_user(DEFAULTS), '')

    def test_a_default_temperature_alone_does_not_make_a_block(self):
        self.assertEqual(about_user(Preferences(default_temperature=0.1)), '')

    def test_what_the_user_said_about_themselves_is_carried(self):
        block = about_user(Preferences(
            timezone='Asia/Kolkata', display_name='Kaushal',
            bio='Backend engineer. Prefers code first.',
        ))
        self.assertIn('Kaushal', block)
        self.assertIn('Asia/Kolkata', block)
        self.assertIn('Prefers code first', block)
        self.assertNotIn('Preferred language', block)

    def test_a_non_default_language_is_an_instruction_that_yields(self):
        block = about_user(Preferences(language='es'))
        self.assertIn('Reply in Spanish', block)
        self.assertIn('unless they write to you in another language', block)

    def test_local_now_is_in_the_users_zone_and_names_it(self):
        noon_utc = datetime(2026, 9, 18, 12, 0, tzinfo=dt_timezone.utc)
        text = local_now(Preferences(timezone='Asia/Kolkata'), noon_utc)
        self.assertIn('05:30 PM', text)
        self.assertIn('Asia/Kolkata', text)


class ReadingTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='prefs', password='pw')
        self.profile, _ = UserProfile.objects.get_or_create(user=self.user)

    def test_no_user_is_the_defaults(self):
        self.assertEqual(for_user(None), DEFAULTS)

    def test_a_zone_saved_before_validation_falls_back_to_utc(self):
        """Junk in the column must not raise inside every turn."""
        UserProfile.objects.filter(pk=self.profile.pk).update(timezone='Mars/Base')
        self.assertEqual(for_user(self.user.id).timezone, 'UTC')

    def test_a_legacy_language_name_is_read_as_its_code(self):
        UserProfile.objects.filter(pk=self.profile.pk).update(language='French')
        self.assertEqual(for_user(self.user.id).language, 'fr')

    def test_a_long_bio_is_cut_on_a_word(self):
        UserProfile.objects.filter(pk=self.profile.pk).update(
            bio='word ' * 400)
        bio = for_user(self.user.id).bio
        self.assertLessEqual(len(bio), MAX_BIO_CHARS + 1)
        self.assertTrue(bio.endswith('…'))
        self.assertNotIn('wor…', bio)


class ProfileEndpointTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username='me', email='me@example.com', password='pw')
        UserProfile.objects.get_or_create(user=self.user)
        self.client.force_authenticate(self.user)

    def test_credits_cannot_be_granted_through_settings(self):
        """They were writable: any user could PATCH an unlimited balance."""
        before = UserProfile.objects.get(user=self.user).credits_remaining
        self.client.patch('/api/auth/profile/', {'credits_remaining': 999_999},
                          format='json')
        after = UserProfile.objects.get(user=self.user).credits_remaining
        self.assertEqual(after, before)

    def test_a_bad_timezone_is_refused(self):
        response = self.client.patch('/api/auth/profile/',
                                     {'timezone': 'Mars/Base'}, format='json')
        self.assertEqual(response.status_code, 400)
        self.assertIn('timezone', response.data)

    def test_a_language_name_is_stored_as_its_code(self):
        response = self.client.patch('/api/auth/profile/',
                                     {'language': 'Spanish'}, format='json')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(UserProfile.objects.get(user=self.user).language, 'es')

    def test_another_accounts_email_cannot_be_claimed(self):
        User.objects.create_user(username='them', email='them@example.com',
                                 password='pw')
        response = self.client.patch(
            '/api/auth/profile/', {'user': {'email': 'THEM@example.com'}},
            format='json')
        self.assertEqual(response.status_code, 400)
        self.user.refresh_from_db()
        self.assertEqual(self.user.email, 'me@example.com')


class WiringTests(TestCase):
    """Where the settings actually land."""

    def setUp(self):
        self.user = User.objects.create_user(username='wired', password='pw')
        UserProfile.objects.get_or_create(user=self.user)
        UserProfile.objects.filter(user=self.user).update(
            timezone='Asia/Kolkata', display_name='Kaushal', language='es')

    def test_chat_puts_the_block_in_the_system_prompt(self):
        from asgiref.sync import async_to_sync

        from chat.turn.pipeline import _user_memory_block

        block = async_to_sync(_user_memory_block)(
            self.user.id, for_user(self.user.id))
        self.assertIn('Kaushal', block)
        self.assertIn('Reply in Spanish', block)

    def test_chats_clock_is_the_users_zone(self):
        from chat.turn.pipeline import _now_string

        self.assertIn('Asia/Kolkata', _now_string(for_user(self.user.id)))

    def test_an_agent_is_told_who_it_works_for_and_when(self):
        from agents.agent.runtime import build_system_prompt
        from agents.models import SubAgent

        agent = SubAgent.objects.create(
            user=self.user, name='Clock', prompt='Say the time.',
            agent_context={'useEnvironment': True},
        )
        gathered = {
            'skills': [], 'knowledge_bases': [], 'ctx': agent.agent_context,
            'user_memory': '', 'preferences': for_user(self.user.id),
        }
        prompt = build_system_prompt(agent, gathered)
        self.assertIn('Asia/Kolkata', prompt)
        self.assertIn('Kaushal', prompt)
        self.assertNotIn('+00:00', prompt)


class ZoneTests(SimpleTestCase):
    def test_zone_validation(self):
        self.assertTrue(zone_is_valid('Asia/Kolkata'))
        self.assertTrue(zone_is_valid('UTC'))
        self.assertFalse(zone_is_valid('Mars/Base'))
        self.assertFalse(zone_is_valid(''))


class EmailChangeTests(APITestCase):
    """The sign-in address changes only once a code sent to it comes back."""

    def setUp(self):
        self.user = User.objects.create_user(
            username='mover', email='old@example.com', password='pw-123456')
        UserProfile.objects.get_or_create(user=self.user)
        self.client.force_authenticate(self.user)

    def _request(self, email='new@example.com', password='pw-123456'):
        from unittest.mock import patch

        sent = {}

        def capture(user, code, purpose, recipient=''):
            sent.update(code=code, recipient=recipient)

        with patch('core.views._send_password_otp_email', capture):
            response = self.client.post('/api/auth/email/change/request/',
                                        {'new_email': email, 'password': password},
                                        format='json')
        return response, sent

    def test_the_profile_patch_no_longer_changes_it(self):
        response = self.client.patch('/api/auth/profile/',
                                     {'user': {'email': 'new@example.com'}},
                                     format='json')
        self.assertEqual(response.status_code, 400)
        self.user.refresh_from_db()
        self.assertEqual(self.user.email, 'old@example.com')

    def test_sending_the_same_address_through_the_profile_is_fine(self):
        """The Settings form always sends it; that must not start failing."""
        response = self.client.patch('/api/auth/profile/',
                                     {'user': {'email': 'OLD@example.com'}},
                                     format='json')
        self.assertEqual(response.status_code, 200)

    def test_the_code_goes_to_the_new_address_and_applies_it(self):
        response, sent = self._request()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(sent['recipient'], 'new@example.com')
        self.user.refresh_from_db()
        self.assertEqual(self.user.email, 'old@example.com')

        confirm = self.client.post('/api/auth/email/change/confirm/',
                                   {'otp_code': sent['code']}, format='json')
        self.assertEqual(confirm.status_code, 200)
        self.user.refresh_from_db()
        self.assertEqual(self.user.email, 'new@example.com')

    def test_a_wrong_code_changes_nothing(self):
        _response, sent = self._request()
        wrong = '000000' if sent['code'] != '000000' else '111111'
        confirm = self.client.post('/api/auth/email/change/confirm/',
                                   {'otp_code': wrong}, format='json')
        self.assertEqual(confirm.status_code, 400)
        self.user.refresh_from_db()
        self.assertEqual(self.user.email, 'old@example.com')

    def test_the_current_password_is_required(self):
        response, sent = self._request(password='wrong')
        self.assertEqual(response.status_code, 400)
        self.assertEqual(sent, {})

    def test_an_address_another_account_holds_is_refused(self):
        User.objects.create_user(username='x', email='new@example.com', password='pw')
        response, sent = self._request()
        self.assertEqual(response.status_code, 400)
        self.assertEqual(sent, {})


class RegistrationEmailTests(APITestCase):
    def test_an_address_already_registered_is_refused(self):
        User.objects.create_user(username='first', email='taken@example.com',
                                 password='pw')
        response = self.client.post('/api/auth/register/', {
            'username': 'second', 'email': 'Taken@example.com',
            'password': 'Sufficiently-long-9', 'password2': 'Sufficiently-long-9',
        }, format='json')
        self.assertEqual(response.status_code, 400)
        self.assertIn('email', response.data)
