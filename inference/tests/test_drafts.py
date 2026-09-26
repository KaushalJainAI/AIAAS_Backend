"""
Office drafts (inference/drafts.py): autosave without rebuilding the file.

What these pin:
* a draft parks the editor state cheaply and changes no bytes;
* the bytes are rebuilt after quiet (and the draft clears);
* a stale draft is refused, while a draft based on the pre-render etag still
  lands after the background render moved `updated_at` under the app;
* one burst of drafts renders once and keeps one version;
* a real overwrite after the render closes the render-door again.
"""
from __future__ import annotations

import shutil
import tempfile
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from inference import drafts, vfs
from inference.models import Document, DocumentVersion

User = get_user_model()

_MEDIA = tempfile.mkdtemp(prefix='drafts-')

DOC_SPEC = {'title': 'Drafted', 'blocks': [{'type': 'paragraph', 'text': 'drafted words'}]}


def _stream(resp) -> bytes:
    return b''.join(resp.streaming_content) if getattr(resp, 'streaming', False) else resp.content


def _stored_bytes(doc_id: int) -> bytes:
    """The bytes as stored, without going through a view that would flush."""
    doc = Document.objects.get(id=doc_id)
    with doc.file.open('rb') as fh:
        return fh.read()


@override_settings(MEDIA_ROOT=_MEDIA)
class DraftCase(TestCase):
    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(_MEDIA, ignore_errors=True)

    def setUp(self):
        self.owner = User.objects.create_user('owner', 'o@example.com', 'pw')
        self.me = APIClient()
        self.me.force_authenticate(self.owner)
        self.scope = vfs.build_scope(self.owner, 'full')

    def new(self, name):
        resp = self.me.post('/api/inference/documents/new/', {'name': name}, format='json')
        self.assertEqual(resp.status_code, 201, resp.content)
        return resp.json()

    def draft(self, doc_id, body, expected=None):
        payload = dict(body)
        if expected is not None:
            payload['expected_updated_at'] = expected
        return self.me.post(f'/api/inference/documents/{doc_id}/draft/', payload, format='json')

    def backdate_draft(self, doc_id, seconds=60):
        doc = Document.objects.get(id=doc_id)
        meta = dict(doc.metadata or {})
        meta['draft']['saved_at'] = (timezone.now() - timedelta(seconds=seconds)).isoformat()
        Document.objects.filter(id=doc_id).update(metadata=meta)


class DraftSaveTests(DraftCase):
    def test_a_draft_parks_state_and_changes_no_bytes(self):
        doc = self.new('Plan.docx')
        before = _stored_bytes(doc['id'])
        resp = self.draft(doc['id'], {'spec': DOC_SPEC}, expected=doc['updated_at'])
        self.assertEqual(resp.status_code, 200, resp.content)
        # Read straight from storage: every view would flush the draft first.
        self.assertEqual(before, _stored_bytes(doc['id']))
        stored = Document.objects.get(id=doc['id'])
        self.assertEqual(stored.metadata['draft']['kind'], 'spec')
        # No version for parking state: nothing was overwritten.
        self.assertEqual(DocumentVersion.objects.filter(document_id=doc['id']).count(), 0)

    def test_a_stale_draft_is_refused(self):
        doc = self.new('Plan.docx')
        resp = self.me.post(
            f'/api/inference/documents/{doc["id"]}/office/',
            {'spec': {'title': 'Elsewhere', 'blocks': [{'type': 'paragraph', 'text': 'x'}]}},
            format='json',
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        resp = self.draft(doc['id'], {'spec': DOC_SPEC}, expected=doc['updated_at'])
        self.assertEqual(resp.status_code, 412)

    def test_a_workbook_draft_needs_sheets(self):
        doc = self.new('B.xlsx')
        resp = self.draft(doc['id'], {'grid': {'nope': []}}, expected=doc['updated_at'])
        self.assertEqual(resp.status_code, 400)

    def test_a_text_file_takes_no_draft(self):
        resp = self.me.post('/api/inference/documents/new/', {'name': 't.txt'}, format='json')
        tid = resp.json()['id']
        resp = self.draft(tid, {'spec': DOC_SPEC})
        self.assertEqual(resp.status_code, 400)


class DraftRenderTests(DraftCase):
    def test_quiet_renders_and_clears(self):
        doc = self.new('Plan.docx')
        before = _stored_bytes(doc['id'])
        self.draft(doc['id'], {'spec': DOC_SPEC}, expected=doc['updated_at'])
        # Not quiet yet: the background task would bow out.
        self.assertFalse(drafts.maybe_render_draft(doc['id']))
        self.backdate_draft(doc['id'])
        self.assertTrue(drafts.maybe_render_draft(doc['id']))
        stored = Document.objects.get(id=doc['id'])
        self.assertNotIn('draft', stored.metadata)
        self.assertEqual(stored.metadata['spec']['title'], 'Drafted')
        after = _stream(self.me.get(f'/api/inference/documents/{doc["id"]}/download/'))
        self.assertNotEqual(before, after)
        # One burst, one version, from the app source.
        versions = DocumentVersion.objects.filter(document_id=doc['id'])
        self.assertEqual(versions.count(), 1)
        self.assertEqual(versions.get().source, 'app')

    def test_a_burst_renders_once(self):
        doc = self.new('Plan.docx')
        first = self.draft(doc['id'], {'spec': DOC_SPEC}, expected=doc['updated_at'])
        stamp = Document.objects.get(id=doc['id']).metadata['draft']['saved_at']
        second = self.draft(
            doc['id'],
            {'spec': {'title': 'Newer', 'blocks': [{'type': 'paragraph', 'text': 'newer'}]}},
            expected=first.json()['updated_at'],
        )
        self.assertEqual(second.status_code, 200, second.content)
        self.backdate_draft(doc['id'])
        # The first task's stamp is superseded by the second draft.
        self.assertFalse(drafts.maybe_render_draft(doc['id'], seen=stamp))
        self.assertTrue(drafts.maybe_render_draft(doc['id']))
        # A second call after the render is a no-op, not a second render.
        self.assertFalse(drafts.maybe_render_draft(doc['id']))
        stored = Document.objects.get(id=doc['id'])
        self.assertEqual(stored.metadata['spec']['title'], 'Newer')
        self.assertEqual(DocumentVersion.objects.filter(document_id=doc['id']).count(), 1)

    def test_a_read_renders_now(self):
        doc = self.new('Plan.docx')
        self.draft(doc['id'], {'spec': DOC_SPEC}, expected=doc['updated_at'])
        # No quiet has passed, but the download needs the bytes.
        stored = Document.objects.get(id=doc['id'])
        drafts.ensure_rendered(stored)
        self.assertNotIn('draft', Document.objects.get(id=doc['id']).metadata)

    def test_a_workbook_grid_renders_cells(self):
        doc = self.new('B.xlsx')
        grid = {'sheets': [{'name': 'Sheet1',
                            'rows': [['Column A', 'Column B', 'Column C'], ['hello', '', '']]}]}
        resp = self.draft(doc['id'], {'grid': grid}, expected=doc['updated_at'])
        self.assertEqual(resp.status_code, 200, resp.content)
        self.backdate_draft(doc['id'])
        self.assertTrue(drafts.maybe_render_draft(doc['id']))
        shown = self.me.get(f'/api/inference/documents/{doc["id"]}/office/').json()
        self.assertIn('hello', shown['sheets'][0]['rows'][1])


class DraftEtagTests(DraftCase):
    def test_a_pre_render_etag_still_lands_after_the_render(self):
        doc = self.new('Plan.docx')
        first = self.draft(doc['id'], {'spec': DOC_SPEC}, expected=doc['updated_at'])
        app_etag = first.json()['updated_at']
        self.backdate_draft(doc['id'])
        self.assertTrue(drafts.maybe_render_draft(doc['id']))
        rendered_etag = Document.objects.get(id=doc['id']).updated_at.isoformat()
        self.assertNotEqual(app_etag, rendered_etag)
        # The app has not seen the render, but nothing else wrote either.
        resp = self.draft(
            doc['id'],
            {'spec': {'title': 'After', 'blocks': [{'type': 'paragraph', 'text': 'after'}]}},
            expected=app_etag,
        )
        self.assertEqual(resp.status_code, 200, resp.content)

    def test_a_real_overwrite_closes_the_render_door(self):
        from office import workbook

        doc = self.new('Plan.docx')
        first = self.draft(doc['id'], {'spec': DOC_SPEC}, expected=doc['updated_at'])
        app_etag = first.json()['updated_at']
        self.backdate_draft(doc['id'])
        self.assertTrue(drafts.maybe_render_draft(doc['id']))
        # An agent replaces the file: the pending lineage is over.
        spec = workbook.validate({'sheets': [{'name': 'S', 'columns': ['A'], 'rows': [['x']]}]})
        data, _ = workbook.render(spec)
        vfs.write_binary(self.scope, '/Plan.docx', data, overwrite=True)
        resp = self.draft(
            doc['id'],
            {'spec': {'title': 'Stale', 'blocks': [{'type': 'paragraph', 'text': 'stale'}]}},
            expected=app_etag,
        )
        self.assertEqual(resp.status_code, 412)

    def test_an_agent_read_sees_rendered_bytes(self):
        doc = self.new('Plan.docx')
        self.draft(doc['id'], {'spec': DOC_SPEC}, expected=doc['updated_at'])
        found = vfs.read_file(self.scope, '/Plan.docx')
        self.assertIn('drafted words', found['content'])


class RenderRaceTests(DraftCase):
    """Review fixes (2026-09-25): the render must not break the app's etag or
    wipe a newer draft."""

    def test_a_full_save_after_the_render_takes_the_pre_render_etag(self):
        # Ctrl+S, restore and import share the render door, not just drafts.
        doc = self.new('Plan.docx')
        app_etag = self.draft(doc['id'], {'spec': DOC_SPEC},
                              expected=doc['updated_at']).json()['updated_at']
        self.backdate_draft(doc['id'])
        self.assertTrue(drafts.maybe_render_draft(doc['id']))
        resp = self.me.post(
            f'/api/inference/documents/{doc["id"]}/office/',
            {'spec': {'title': 'Saved', 'blocks': [{'type': 'paragraph', 'text': 'saved'}]},
             'expected_updated_at': app_etag},
            format='json',
        )
        self.assertEqual(resp.status_code, 200, resp.content)

    def test_a_draft_saved_mid_render_survives_it(self):
        from inference.office_edit import DraftSuperseded

        doc = self.new('Plan.docx')
        first = self.draft(doc['id'], {'spec': DOC_SPEC}, expected=doc['updated_at'])
        rendering = Document.objects.get(id=doc['id'])
        old_draft = dict(rendering.metadata['draft'])
        bytes_before = _stored_bytes(doc['id'])
        # The app saves again while the render of the first draft is running.
        newer = {'title': 'Newer', 'blocks': [{'type': 'paragraph', 'text': 'newer words'}]}
        second = self.draft(doc['id'], {'spec': newer}, expected=first.json()['updated_at'])
        self.assertEqual(second.status_code, 200, second.content)
        with self.assertRaises(DraftSuperseded):
            drafts._render(rendering, old_draft)
        stored = Document.objects.get(id=doc['id'])
        self.assertEqual(stored.metadata['draft']['payload']['title'], 'Newer')
        self.assertEqual(_stored_bytes(doc['id']), bytes_before)
        # And the app's next save still lands on the etag it holds.
        third = self.draft(doc['id'], {'spec': newer}, expected=second.json()['updated_at'])
        self.assertEqual(third.status_code, 200, third.content)

    def test_a_rename_leaves_the_etag_alone(self):
        doc = self.new('Plan.docx')
        resp = self.me.patch(f'/api/inference/documents/{doc["id"]}/',
                             {'name': 'Renamed.docx'}, format='json')
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(resp.json()['updated_at'], doc['updated_at'])
        resp = self.draft(doc['id'], {'spec': DOC_SPEC}, expected=doc['updated_at'])
        self.assertEqual(resp.status_code, 200, resp.content)
