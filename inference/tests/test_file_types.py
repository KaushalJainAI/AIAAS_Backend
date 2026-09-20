"""
Which files may be uploaded, and what we can read out of them (2026-09-20).

The library used to be smaller than the platform: an agent could *write* a
workbook that its owner could not *upload*, and every format without a parser
was refused outright. Uploads are now allowed by default and refused by
exception (executables), and a format with no reader is kept with no text
rather than with the mojibake that reading a zip as UTF-8 produces.

The files under test are produced by the office renderers themselves, so the
readers are exercised against real `.xlsx`/`.pptx`/`.docx`, not fixtures that
could drift from what the app writes.
"""
from __future__ import annotations

import shutil
import tempfile

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings

from chat.tools.office import deck, document, workbook
from inference.utils import (
    DocumentProcessor, extract_pptx_text, extract_text_from_file,
    extract_xlsx_text, normalize_file_type,
)

User = get_user_model()


def a_workbook() -> bytes:
    return workbook.render(workbook.validate({'sheets': [{
        'name': 'Sales', 'columns': [{'header': 'Region'}, {'header': 'Revenue', 'type': 'number'}],
        'rows': [['North', 120], ['South', 95]], 'totals': True,
    }]}))[0]


def a_deck() -> bytes:
    return deck.render(deck.validate({'slides': [
        {'layout': 'title', 'title': 'Quarterly review'},
        {'layout': 'bullets', 'title': 'Findings', 'bullets': ['Revenue grew'], 'notes': 'Mention Pune'},
    ]}), {})


def a_document() -> bytes:
    return document.render(document.validate({'title': 'Memo', 'blocks': [
        {'type': 'paragraph', 'text': 'Downtime was 47 minutes.'}]}), {})


class VocabularyTests(TestCase):
    def test_office_extensions_have_their_own_type(self):
        self.assertEqual(normalize_file_type('q3.xlsx'), 'xlsx')
        self.assertEqual(normalize_file_type('deck.pptx'), 'pptx')
        self.assertEqual(normalize_file_type('memo.docx'), 'docx')
        self.assertEqual(normalize_file_type('song.mp3'), 'audio')

    def test_an_unknown_binary_is_other_not_text(self):
        # `txt` was the old default, and it is what sent zip noise into the
        # search index.
        self.assertEqual(normalize_file_type('archive.zip'), 'other')
        self.assertEqual(normalize_file_type('model.parquet'), 'other')

    def test_a_sniffed_text_type_with_no_extension_is_still_text(self):
        self.assertEqual(normalize_file_type('README', 'text/plain'), 'txt')


class UploadPolicyTests(TestCase):
    def validate(self, name, data, content_type='application/octet-stream'):
        return DocumentProcessor.validate_file_upload(
            SimpleUploadedFile(name, data, content_type=content_type))

    def test_office_files_are_accepted(self):
        for name, data in (('q3.xlsx', a_workbook()), ('deck.pptx', a_deck()), ('memo.docx', a_document())):
            with self.subTest(name=name):
                self.assertTrue(self.validate(name, data))

    def test_a_format_with_no_reader_is_accepted_and_kept(self):
        # The point of the change: `execute_python` can open it later.
        self.assertTrue(self.validate('data.parquet', b'PAR1\x00\x01binary'))

    def test_executables_are_refused(self):
        with self.assertRaisesRegex(ValidationError, 'Executable'):
            self.validate('setup.exe', b'MZ\x90\x00binary')
        with self.assertRaisesRegex(ValidationError, 'Executable'):
            self.validate('app.msi', b'\xd0\xcf\x11\xe0binary')

    def test_the_size_cap_still_applies(self):
        from workflow_backend.thresholds import MAX_DOCUMENT_SIZE

        with self.assertRaisesRegex(ValidationError, 'too large'):
            self.validate('big.bin', b'x' * (MAX_DOCUMENT_SIZE + 1))


class ExtractionTests(TestCase):
    def setUp(self):
        self._dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self._dir, True)

    def write(self, name, data) -> str:
        path = f'{self._dir}/{name}'
        with open(path, 'wb') as handle:
            handle.write(data)
        return path

    def test_a_workbook_reads_back_as_sheets_headers_and_cells(self):
        text = extract_xlsx_text(self.write('q3.xlsx', a_workbook()))
        self.assertIn('# Sheet: Sales', text)
        self.assertIn('Region | Revenue', text)
        self.assertIn('North | 120', text)
        # A formula is indexed as what the cell holds, not as a guessed value.
        self.assertIn('=SUM(B2:B3)', text)

    def test_a_deck_reads_back_as_slides_and_notes(self):
        text = extract_pptx_text(self.write('deck.pptx', a_deck()))
        self.assertIn('# Slide 1', text)
        self.assertIn('Quarterly review', text)
        self.assertIn('Notes: Mention Pune', text)

    def test_the_dispatcher_routes_each_office_type(self):
        cases = [('q3.xlsx', a_workbook(), 'xlsx', 'North'),
                 ('deck.pptx', a_deck(), 'pptx', 'Findings'),
                 ('memo.docx', a_document(), 'docx', 'Downtime')]
        for name, data, file_type, needle in cases:
            with self.subTest(file_type=file_type):
                self.assertIn(needle, extract_text_from_file(self.write(name, data), file_type))

    def test_an_unreadable_format_yields_no_text_rather_than_noise(self):
        path = self.write('data.parquet', b'PAR1\x00\x02\x03rubbish\xff\xfe')
        self.assertEqual(extract_text_from_file(path, 'other'), '')

    def test_a_corrupt_office_file_is_empty_not_an_exception(self):
        path = self.write('broken.xlsx', b'not a zip at all')
        self.assertEqual(extract_xlsx_text(path), '')
        self.assertEqual(extract_pptx_text(path), '')


@override_settings(MEDIA_ROOT=tempfile.mkdtemp())
class KeptFilesReachPythonTests(TestCase):
    """A format we cannot read is still usable — that is the deal being made."""

    def setUp(self):
        self.user = User.objects.create_user('owner', 'owner@example.com', 'pw')

    def test_an_unreadable_upload_is_handed_to_the_sandbox_as_bytes(self):
        from django.core.files.base import ContentFile

        from chat.tools.sandbox import _load_inputs
        from inference import vfs
        from inference.models import Document

        scope = vfs.chat_scope(self.user)
        folder = vfs._folder_at(scope, ['Chat'])
        doc = Document(user=self.user, folder=folder, name='data.parquet',
                       file_type='other', file_size=7, status='stored')
        doc.file.save('data.parquet', ContentFile(b'PAR1\x00\x01'), save=False)
        doc.save()

        self.assertTrue(vfs.is_binary(doc))
        self.assertEqual(_load_inputs(scope, ['/Chat/data.parquet']),
                         {'data.parquet': b'PAR1\x00\x01'})
