"""
In-browser suite doors: `PATCH documents/<id>/content/`, `?inline=1`
downloads, and the dashboard CRUD (`inference/dashboard_views.py`).

Pins the P1/P2 contract: text saves round-trip with a stale-write guard,
binaries refuse with a re-render pointer, inline serves preview bytes while
the default still forces a save, and foreign dashboard rows are the same 404.
"""
from __future__ import annotations

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from inference.models import Dashboard, Document

User = get_user_model()


class SuiteCase(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user('owner', 'o@example.com', 'pw')
        self.other = User.objects.create_user('other', 't@example.com', 'pw')
        self.me = APIClient()
        self.me.force_authenticate(self.owner)
        self.them = APIClient()
        self.them.force_authenticate(self.other)

    def doc(self, **kw):
        params = {
            'user': self.owner, 'name': 'notes.md', 'file_type': 'md',
            'file_size': 2, 'content_text': 'hi', 'status': 'stored',
        }
        params.update(kw)
        return Document.objects.create(**params)


class DocumentContentTests(SuiteCase):
    def test_text_save_round_trips(self):
        doc = self.doc()
        resp = self.me.patch(
            f'/api/inference/documents/{doc.id}/content/',
            {'content': 'hello edited'}, format='json')
        self.assertEqual(resp.status_code, 200)
        doc.refresh_from_db()
        self.assertEqual(doc.content_text, 'hello edited')

    def test_stale_write_is_412(self):
        doc = self.doc()
        resp = self.me.patch(
            f'/api/inference/documents/{doc.id}/content/',
            {'content': 'stale', 'expected_updated_at': '2000-01-01T00:00:00+00:00'},
            format='json')
        self.assertEqual(resp.status_code, 412)
        doc.refresh_from_db()
        self.assertEqual(doc.content_text, 'hi')

    def test_matching_etag_saves(self):
        doc = self.doc()
        resp = self.me.patch(
            f'/api/inference/documents/{doc.id}/content/',
            {'content': 'fresh', 'expected_updated_at': doc.updated_at.isoformat()},
            format='json')
        self.assertEqual(resp.status_code, 200)

    def test_binary_is_refused(self):
        doc = self.doc(name='deck.pptx', file_type='pptx', file='x')
        resp = self.me.patch(
            f'/api/inference/documents/{doc.id}/content/',
            {'content': 'nope'}, format='json')
        self.assertEqual(resp.status_code, 400)

    def test_foreign_doc_is_404(self):
        doc = self.doc()
        resp = self.them.patch(
            f'/api/inference/documents/{doc.id}/content/',
            {'content': 'mine?'}, format='json')
        self.assertEqual(resp.status_code, 404)


class InlineDownloadTests(SuiteCase):
    def test_default_is_attachment_inline_is_preview(self):
        doc = self.doc(content_text='hello')
        default = self.me.get(f'/api/inference/documents/{doc.id}/download/')
        self.assertIn('attachment', default['Content-Disposition'])
        inline = self.me.get(f'/api/inference/documents/{doc.id}/download/?inline=1')
        self.assertIn('inline', inline['Content-Disposition'])


class DashboardApiTests(SuiteCase):
    def _payload(self, **kw):
        body = {
            'title': 'Ops', 'tiles': [{'kind': 'kpi', 'title': 'MRR', 'value': '9'}],
            'visibility': 'platform',
        }
        body.update(kw)
        return body

    def test_create_list_refresh_delete(self):
        created = self.me.post('/api/inference/dashboards/', self._payload(), format='json')
        self.assertEqual(created.status_code, 201)
        did = created.json()['id']

        listed = self.me.get('/api/inference/dashboards/')
        self.assertTrue(any(r['id'] == did for r in listed.json()['results']))

        refreshed = self.me.post(f'/api/inference/dashboards/{did}/refresh/')
        self.assertEqual(refreshed.status_code, 200)
        self.assertIn('refreshed_at', refreshed.json())

        updated = self.me.patch(f'/api/inference/dashboards/{did}/',
                                {'title': 'Ops v2', 'tiles': [{'kind': 'text', 'text': 'hi'}]},
                                format='json')
        self.assertEqual(updated.status_code, 200)
        self.assertEqual(Dashboard.objects.get(id=did).title, 'Ops v2')

        deleted = self.me.delete(f'/api/inference/dashboards/{did}/')
        self.assertEqual(deleted.status_code, 204)

    def test_bad_spec_is_400_and_foreign_is_404(self):
        bad = self.me.post('/api/inference/dashboards/', {'title': '', 'tiles': []}, format='json')
        self.assertEqual(bad.status_code, 400)
        mine = self.me.post('/api/inference/dashboards/', self._payload(), format='json').json()
        foreign = self.them.get(f"/api/inference/dashboards/{mine['id']}/")
        # platform-visible: readable, but not writable by a stranger.
        self.assertEqual(foreign.status_code, 200)
        refused = self.them.patch(f"/api/inference/dashboards/{mine['id']}/", {'title': 'x'},
                                  format='json')
        self.assertEqual(refused.status_code, 404)
