"""
Hosted pages: snapshots shareable by link (`inference/pages.py`, `page_views.py`,
the `publish_page` tool).

What is pinned is what makes a public surface safe to have: every refusal is
the same 404 (so the endpoint is not an oracle for what people published
privately), each visibility reaches exactly as far as its name says, a page is
a snapshot rather than a pointer, withdrawing unlists rather than deletes, and
an unattended run cannot publish to the open internet.
"""
from __future__ import annotations

import json
import shutil
import tempfile

from asgiref.sync import async_to_sync
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from chat.tools import execute_tool
from inference import vfs
from inference.models import PublishedPage
from inference.pages import PublishError, publish

User = get_user_model()


class PageTestCase(TestCase):
    def setUp(self):
        self._media = tempfile.mkdtemp()
        self._override = override_settings(MEDIA_ROOT=self._media)
        self._override.enable()
        self.owner = User.objects.create_user('owner', 'owner@example.com', 'pw')
        self.other = User.objects.create_user('other', 'other@example.com', 'pw')
        self.anon = APIClient()
        self.me = APIClient()
        self.me.force_authenticate(self.owner)
        self.them = APIClient()
        self.them.force_authenticate(self.other)

    def tearDown(self):
        self._override.disable()
        shutil.rmtree(self._media, ignore_errors=True)

    def page(self, visibility='platform', **kw):
        return publish(self.owner, title=kw.pop('title', 'Q3 review'), kind=kw.pop('kind', 'report'),
                       body=kw.pop('body', '# Q3\nRevenue grew.'), visibility=visibility)


class VisibilityTests(PageTestCase):
    def test_public_is_readable_with_no_account(self):
        page = self.page('public')
        resp = self.anon.get(f'/api/inference/public/pages/{page.slug}/')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()['title'], 'Q3 review')
        self.assertNotIn('is_mine', resp.json())  # the anonymous projection

    def test_every_non_public_refusal_is_the_same_404(self):
        link, platform = self.page('link'), self.page('platform', title='Other')
        withdrawn = self.page('public', title='Gone')
        self.me.delete(f'/api/inference/pages/{withdrawn.slug}/')
        bodies = set()
        for slug in (link.slug, platform.slug, withdrawn.slug, 'never-existed'):
            resp = self.anon.get(f'/api/inference/public/pages/{slug}/')
            self.assertEqual(resp.status_code, 404, slug)
            bodies.add(json.dumps(resp.json()))
        self.assertEqual(len(bodies), 1)

    def test_signed_in_readers_reach_link_and_platform_pages(self):
        for vis in ('link', 'platform'):
            page = self.page(vis, title=f'Page {vis}')
            self.assertEqual(self.them.get(f'/api/inference/pages/{page.slug}/').status_code, 200)

    def test_platform_listing_shows_listed_pages_but_never_link_ones(self):
        self.page('link', title='Secret link')
        self.page('platform', title='For everyone')
        titles = {p['title'] for p in self.them.get('/api/inference/pages/?scope=platform').json()['results']}
        self.assertEqual(titles, {'For everyone'})

    def test_public_responses_carry_a_csp(self):
        page = self.page('public')
        resp = self.anon.get(f'/api/inference/public/pages/{page.slug}/')
        self.assertIn("default-src 'none'", resp['Content-Security-Policy'])


class LifecycleTests(PageTestCase):
    def test_withdraw_unlists_rather_than_deletes(self):
        page = self.page('public')
        self.assertEqual(self.me.delete(f'/api/inference/pages/{page.slug}/').status_code, 204)
        page.refresh_from_db()
        self.assertFalse(page.is_listed)
        self.assertIsNotNone(page.withdrawn_at)

    def test_only_the_owner_can_withdraw(self):
        page = self.page('platform')
        self.assertEqual(self.them.delete(f'/api/inference/pages/{page.slug}/').status_code, 404)
        page.refresh_from_db()
        self.assertTrue(page.is_listed)

    def test_slugs_do_not_collide(self):
        self.assertNotEqual(self.page().slug, self.page().slug)

    def test_a_file_page_is_a_snapshot_not_a_pointer(self):
        scope = vfs.chat_scope(self.owner)
        written = vfs.write_file(scope, '/Chat/notes.md', 'version one')
        page = publish(self.owner, title='Notes', kind='file',
                       body=json.dumps({'document_id': written['document_id']}), visibility='public')
        vfs.edit_file(scope, '/Chat/notes.md', 'one', 'two')
        resp = self.anon.get(f'/api/inference/public/pages/{page.slug}/download/')
        self.assertEqual(b''.join(resp.streaming_content), b'version one')

    def test_you_cannot_publish_someone_elses_file(self):
        written = vfs.write_file(vfs.chat_scope(self.other), '/Chat/theirs.md', 'private')
        with self.assertRaisesRegex(PublishError, 'No such file'):
            publish(self.owner, title='x', kind='file',
                    body=json.dumps({'document_id': written['document_id']}), visibility='public')

    def test_the_api_and_the_tool_share_one_validation(self):
        resp = self.me.post('/api/inference/pages/', {'title': 'x', 'kind': 'video', 'body': ''}, format='json')
        self.assertEqual(resp.status_code, 400)
        self.assertIn('Unknown kind', resp.json()['error'])


class PublishToolTests(PageTestCase):
    def call(self, args, caller='chat'):
        ctx = {'user_id': self.owner.id, 'caller': caller}
        return json.loads(async_to_sync(execute_tool)('publish_page', args, ctx))

    def test_publishes_and_returns_the_link(self):
        out = self.call({'title': 'Launch notes', 'kind': 'report', 'body': '# Hi', 'visibility': 'link'})
        self.assertEqual(out['url'], '/p/launch-notes')
        self.assertIn('[Launch notes](/p/launch-notes)', out['rendered'])

    def test_an_unattended_run_cannot_publish_wider_than_link(self):
        out = self.call({'title': 'x', 'kind': 'report', 'body': 'y', 'visibility': 'public'}, caller='trigger')
        self.assertIn('unattended', out['error'])
        self.assertFalse(PublishedPage.objects.exists())

    def test_it_is_gated_as_outward_facing(self):
        from chat.tools.registry import get

        tool = get('publish_page')
        self.assertTrue(tool.sensitive)
        self.assertEqual(tool.effect, 'irreversible')
