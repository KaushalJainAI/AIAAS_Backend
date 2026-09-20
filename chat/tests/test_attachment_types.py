"""
What a file attached to a message is read as (`chat/sources/attachments.py`).

Word and Excel were missing from the classifier until 2026-09-20: both landed
as `other` and extracted to an empty string, so attaching a spreadsheet gave
the model its filename and nothing else while the UI showed an attachment. A
format with no reader still extracts nothing — that part is deliberate — but
its bytes are kept, and `run_python_on_files` is how it gets opened.
"""
from __future__ import annotations

from django.test import SimpleTestCase

from chat.sources.attachments import classify_file, extract_text
from chat.tools.office import deck, document, workbook


def a_workbook() -> bytes:
    return workbook.render(workbook.validate({'sheets': [{
        'name': 'Q3', 'columns': [{'header': 'Region'}, {'header': 'Units', 'type': 'integer'}],
        'rows': [['North', 120]],
    }]}))[0]


def a_document() -> bytes:
    return document.render(document.validate({'title': 'Memo', 'blocks': [
        {'type': 'heading', 'text': 'Summary'},
        {'type': 'paragraph', 'text': 'Revenue grew 38%.'}]}), {})


def a_deck() -> bytes:
    return deck.render(deck.validate({'slides': [{'layout': 'title', 'title': 'Board update'}]}), {})


class ClassifyTests(SimpleTestCase):
    def test_office_files_are_recognised(self):
        self.assertEqual(classify_file('q3.xlsx'), 'xlsx')
        self.assertEqual(classify_file('notes.docx'), 'docx')
        self.assertEqual(classify_file('deck.pptx'), 'pptx')

    def test_the_older_binary_formats_share_their_modern_bucket(self):
        # They classify, and then fail to *read* honestly (see below) — which
        # is better than being invisible.
        self.assertEqual(classify_file('legacy.xls'), 'xlsx')
        self.assertEqual(classify_file('legacy.doc'), 'docx')

    def test_config_text_counts_as_text(self):
        self.assertEqual(classify_file('compose.yml'), 'text')

    def test_an_unknown_format_is_other(self):
        self.assertEqual(classify_file('data.parquet'), 'other')


class ExtractTests(SimpleTestCase):
    def test_a_spreadsheet_arrives_as_its_cells(self):
        text = extract_text(a_workbook(), 'xlsx')
        self.assertIn('# Sheet: Q3', text)
        self.assertIn('North | 120', text)

    def test_a_word_file_arrives_as_its_text(self):
        self.assertIn('Revenue grew 38%.', extract_text(a_document(), 'docx'))

    def test_a_deck_still_arrives_as_its_slides(self):
        self.assertIn('Board update', extract_text(a_deck(), 'pptx'))

    def test_a_format_with_no_reader_extracts_nothing_rather_than_noise(self):
        self.assertEqual(extract_text(b'PAR1\x00\x01\xff', 'other'), '')

    def test_a_corrupt_office_file_is_empty_not_an_exception(self):
        self.assertEqual(extract_text(b'not a zip', 'xlsx'), '')
        self.assertEqual(extract_text(b'not a zip', 'docx'), '')

    def test_extracted_types_ride_in_as_text_for_every_model(self):
        # A model without image support can still be sent these, because they
        # are tokens by the time they reach it.
        from chat.turn.history import _TEXT_EXTRACTED_TYPES

        self.assertTrue({'docx', 'xlsx', 'pptx', 'pdf', 'text'} <= _TEXT_EXTRACTED_TYPES)
