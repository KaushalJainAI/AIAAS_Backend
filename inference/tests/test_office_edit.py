"""
The productivity apps' doors: new files, rename, office edits, shared reads,
and the two save bugs that made in-browser editing look broken.

* The stale-write guard compared `updated_at` as *strings*; the browser sends
  DRF's `...Z` form and the server wrote `+00:00`, so every UI save was a 412.
* An uploaded text file keeps its bytes in `file`, which `download` prefers —
  so an edit written only to `content_text` never showed in preview or export.
"""
from __future__ import annotations

import io
import shutil
import tempfile

from django.contrib.auth import get_user_model
from django.core.files.base import ContentFile
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from inference.models import Document

User = get_user_model()

_MEDIA = tempfile.mkdtemp(prefix='office-edit-')


@override_settings(MEDIA_ROOT=_MEDIA)
class OfficeCase(TestCase):
    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(_MEDIA, ignore_errors=True)

    def setUp(self):
        self.owner = User.objects.create_user('owner', 'o@example.com', 'pw')
        self.other = User.objects.create_user('other', 't@example.com', 'pw')
        self.me = APIClient()
        self.me.force_authenticate(self.owner)
        self.them = APIClient()
        self.them.force_authenticate(self.other)

    def new(self, name, **extra):
        resp = self.me.post('/api/inference/documents/new/', {'name': name, **extra}, format='json')
        self.assertEqual(resp.status_code, 201, resp.content)
        return resp.json()

    def body(self, doc_id, client=None):
        resp = (client or self.me).get(f'/api/inference/documents/{doc_id}/download/')
        if not getattr(resp, 'streaming', False):
            return b'', resp.status_code
        return b''.join(resp.streaming_content), resp.status_code


class SaveGuardTests(OfficeCase):
    def test_the_timestamp_the_api_sends_is_accepted_back(self):
        doc = self.new('notes.md', content='hi')
        resp = self.me.patch(
            f'/api/inference/documents/{doc["id"]}/content/',
            {'content': 'edited', 'expected_updated_at': doc['updated_at']}, format='json')
        self.assertEqual(resp.status_code, 200, resp.content)

    def test_an_edit_to_an_uploaded_text_file_reaches_its_bytes(self):
        doc = Document(user=self.owner, name='a.md', file_type='md', content_text='old',
                       file_size=3, status='indexed')
        doc.file.save('a.md', ContentFile(b'old'), save=True)
        resp = self.me.patch(f'/api/inference/documents/{doc.id}/content/',
                             {'content': 'new text'}, format='json')
        self.assertEqual(resp.status_code, 200)
        body, _ = self.body(doc.id)
        self.assertEqual(body, b'new text')


class NewFileTests(OfficeCase):
    def test_text_file_is_stored_not_indexed(self):
        doc = self.new('todo.md', content='- [ ] a')
        self.assertEqual(doc['status'], 'stored')
        self.assertEqual(doc['file_type'], 'md')

    def test_a_taken_name_is_numbered(self):
        self.new('a.csv')
        self.assertEqual(self.new('a.csv')['filename'], 'a (2).csv')

    def test_unknown_extension_is_refused(self):
        resp = self.me.post('/api/inference/documents/new/', {'name': 'x.exe'}, format='json')
        self.assertEqual(resp.status_code, 400)

    def test_blank_office_files_render(self):
        for name in ('Plan.docx', 'Pitch.pptx', 'Budget.xlsx'):
            doc = self.new(name)
            body, status = self.body(doc['id'])
            self.assertEqual(status, 200)
            self.assertTrue(body.startswith(b'PK'), name)  # a zip, i.e. real OOXML

    def test_foreign_folder_is_404(self):
        from inference.models import Folder

        f = Folder.objects.create(user=self.other, name='theirs', path='/')
        resp = self.me.post('/api/inference/documents/new/',
                            {'name': 'x.md', 'folder_id': f.id}, format='json')
        self.assertEqual(resp.status_code, 404)


class RenameTests(OfficeCase):
    def test_rename_keeps_the_extension(self):
        doc = self.new('a.md')
        ok = self.me.patch(f'/api/inference/documents/{doc["id"]}/', {'name': 'b.md'}, format='json')
        self.assertEqual(ok.json()['filename'], 'b.md')
        bad = self.me.patch(f'/api/inference/documents/{doc["id"]}/', {'name': 'b.txt'}, format='json')
        self.assertEqual(bad.status_code, 400)

    def test_rename_onto_a_sibling_is_409(self):
        self.new('a.md')
        b = self.new('b.md')
        resp = self.me.patch(f'/api/inference/documents/{b["id"]}/', {'name': 'a.md'}, format='json')
        self.assertEqual(resp.status_code, 409)

    def test_foreign_rename_is_404(self):
        doc = self.new('a.md')
        resp = self.them.patch(f'/api/inference/documents/{doc["id"]}/', {'name': 'z.md'}, format='json')
        self.assertEqual(resp.status_code, 404)


class WorkbookTests(OfficeCase):
    def test_grid_then_cell_edit_round_trips(self):
        doc = self.new('Budget.xlsx')
        grid = self.me.get(f'/api/inference/documents/{doc["id"]}/office/').json()
        self.assertEqual(grid['sheets'][0]['rows'][0][:3], ['Column A', 'Column B', 'Column C'])

        resp = self.me.post(f'/api/inference/documents/{doc["id"]}/office/', {
            'sheet': 'Sheet1',
            'set_cells': [{'cell': 'A2', 'value': 'Rent'}, {'cell': 'B2', 'value': 1200}],
            'expected_updated_at': grid['updated_at'],
        }, format='json')
        self.assertEqual(resp.status_code, 200, resp.content)
        resp = self.me.post(f'/api/inference/documents/{doc["id"]}/office/', {
            'append_rows': [['Food', 300]],
            'expected_updated_at': resp.json()['updated_at'],
        }, format='json')
        self.assertEqual(resp.status_code, 200, resp.content)

        grid = self.me.get(f'/api/inference/documents/{doc["id"]}/office/').json()
        rows = grid['sheets'][0]['rows']
        self.assertEqual(rows[1][:2], ['Rent', 1200])
        self.assertEqual(rows[2][:2], ['Food', 300])

    def test_external_formula_is_refused(self):
        doc = self.new('Budget.xlsx')
        resp = self.me.post(f'/api/inference/documents/{doc["id"]}/office/', {
            'set_cells': [{'cell': 'A1', 'value': '=WEBSERVICE("http://x")'}],
        }, format='json')
        self.assertEqual(resp.status_code, 400)

    def test_stale_edit_is_412(self):
        doc = self.new('Budget.xlsx')
        resp = self.me.post(f'/api/inference/documents/{doc["id"]}/office/', {
            'set_cells': [{'cell': 'A1', 'value': 'x'}],
            'expected_updated_at': '2000-01-01T00:00:00Z',
        }, format='json')
        self.assertEqual(resp.status_code, 412)


class SpecEditTests(OfficeCase):
    def test_deck_edit_re_renders(self):
        doc = self.new('Pitch.pptx')
        spec = Document.objects.get(id=doc['id']).metadata['spec']
        spec['slides'].append({'layout': 'bullets', 'title': 'Why now',
                               'bullets': [{'text': 'Market is ready', 'level': 0}]})
        resp = self.me.post(f'/api/inference/documents/{doc["id"]}/office/',
                            {'spec': spec}, format='json')
        self.assertEqual(resp.status_code, 200, resp.content)
        stored = Document.objects.get(id=doc['id'])
        self.assertEqual(len(stored.metadata['spec']['slides']), 2)
        self.assertIn('Market is ready', stored.content_text)

    def test_document_edit_re_renders(self):
        doc = self.new('Plan.docx')
        spec = Document.objects.get(id=doc['id']).metadata['spec']
        spec['blocks'] = [{'type': 'heading', 'text': 'Goals', 'level': 1},
                          {'type': 'paragraph', 'text': 'Ship the apps.'}]
        resp = self.me.post(f'/api/inference/documents/{doc["id"]}/office/',
                            {'spec': spec}, format='json')
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertIn('Ship the apps.', Document.objects.get(id=doc['id']).content_text)

    def test_uploaded_deck_without_spec_is_refused(self):
        doc = Document(user=self.owner, name='up.pptx', file_type='pptx', status='stored', file_size=4)
        doc.file.save('up.pptx', ContentFile(b'PK..'), save=True)
        resp = self.me.post(f'/api/inference/documents/{doc.id}/office/',
                            {'spec': {'slides': []}}, format='json')
        self.assertEqual(resp.status_code, 400)


class SharedReadTests(OfficeCase):
    def test_a_shared_file_can_be_read_but_not_written(self):
        doc = Document.objects.create(user=self.owner, name='pub.md', file_type='md',
                                      content_text='public', status='stored', file_size=6,
                                      sharing_mode='shared_read')
        body, status = self.body(doc.id, client=self.them)
        self.assertEqual((status, body), (200, b'public'))
        self.assertEqual(self.them.get(f'/api/inference/documents/{doc.id}/').status_code, 200)
        resp = self.them.patch(f'/api/inference/documents/{doc.id}/content/',
                               {'content': 'mine'}, format='json')
        self.assertEqual(resp.status_code, 404)

    def test_a_private_file_stays_private(self):
        doc = Document.objects.create(user=self.owner, name='p.md', file_type='md',
                                      content_text='secret', status='stored', file_size=6)
        _, status = self.body(doc.id, client=self.them)
        self.assertEqual(status, 404)


class TypesFilterTests(OfficeCase):
    def test_types_narrows_the_whole_tree(self):
        self.new('a.csv')
        self.new('b.md')
        resp = self.me.get('/api/inference/documents/', {'scope': 'personal', 'types': 'csv'})
        names = [d['filename'] for d in resp.json()['my_documents']]
        self.assertEqual(names, ['a.csv'])


class CopyTests(OfficeCase):
    def test_copy_in_place_is_named_like_a_desktop_copy(self):
        doc = self.new('Pitch.pptx')
        resp = self.me.post(f'/api/inference/documents/{doc["id"]}/copy/', {}, format='json')
        self.assertEqual(resp.status_code, 201, resp.content)
        clone = Document.objects.get(id=resp.json()['id'])
        self.assertEqual(clone.name, 'Pitch - Copy.pptx')
        self.assertIn('spec', clone.metadata)
        self.assertEqual(self.body(clone.id)[0], self.body(doc['id'])[0])

    def test_foreign_copy_is_404(self):
        doc = self.new('a.md')
        resp = self.them.post(f'/api/inference/documents/{doc["id"]}/copy/', {}, format='json')
        self.assertEqual(resp.status_code, 404)

    def test_a_chart_slide_survives_an_edit(self):
        # The stored chart is `build_spec` output; re-validating it must accept it.
        from chat.tools.office import deck

        spec = deck.validate({'title': 'T', 'slides': [
            {'layout': 'chart', 'title': 'Sales', 'chart': {
                'kind': 'bar', 'title': 'Sales',
                'series': [{'name': 'Q', 'points': [{'x': 'A', 'y': 1}, {'x': 'B', 'y': 2}]}]}},
        ]})
        again = deck.validate({'title': 'T', 'slides': deck.preview(spec)['slides']})
        self.assertEqual(again['slides'][0]['chart'], spec['slides'][0]['chart'])
