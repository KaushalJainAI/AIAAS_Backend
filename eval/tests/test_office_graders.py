"""
The office graders open rendered files with their real readers.

The cases worth pinning are the ones where a weaker grader would be fooled: a
workbook whose totals were typed in (right numbers, no formulas), a deck that
names a number without charting it, a text file saved under a `.xlsx` name —
and a formula that tries to run something other than arithmetic.
"""
from __future__ import annotations

from asgiref.sync import async_to_sync
from django.test import SimpleTestCase

from chat.tools.office import deck, document, workbook
from eval import graders, office_files


def grade(spec, **binaries):
    ctx = graders.GradeContext(binaries=binaries)
    [g], _, _ = async_to_sync(graders.grade_all)([spec], ctx)
    return g


def book(rows, totals=True):
    spec = workbook.validate({'sheets': [
        {'name': 'Summary', 'columns': [{'header': 'Region'},
                                        {'header': 'Revenue', 'type': 'number'}],
         'rows': rows, 'totals': totals},
        {'name': 'Data', 'columns': [{'header': 'x', 'type': 'number'}], 'rows': [[5], [7]]},
    ]})
    return workbook.render(spec)[0]


class XlsxValueTests(SimpleTestCase):
    def test_a_formula_is_evaluated_to_its_value(self):
        data = book([['North', 10], ['South', 20]])
        g = grade({'type': 'xlsx_value', 'path': 'b.xlsx', 'sheet': 'Summary',
                   'match': {'Region': 'Total'}, 'column': 'Revenue', 'equals': 30,
                   'formula': True}, **{'b.xlsx': data})
        self.assertTrue(g.passed, g.detail)

    def test_typed_in_totals_fail_the_formula_check_even_when_right(self):
        data = book([['North', 10], ['South', 20], ['Total', 30]], totals=False)
        g = grade({'type': 'xlsx_value', 'path': 'b.xlsx', 'sheet': 'Summary',
                   'match': {'Region': 'Total'}, 'column': 'Revenue', 'equals': 30,
                   'formula': True}, **{'b.xlsx': data})
        self.assertFalse(g.passed)
        self.assertIn('not a formula', g.detail)

    def test_arithmetic_ranges_and_other_sheets(self):
        spec = workbook.validate({'sheets': [
            {'name': 'Data', 'columns': [{'header': 'x', 'type': 'number'}], 'rows': [[5], [7]]},
            {'name': 'Calc', 'columns': [{'header': 'v', 'type': 'number'}], 'rows': [
                ['=SUM(Data!A2:A3)*2-1'], ['=ROUND(AVERAGE(Data!A2:A3)/3, 2)'], ['=MAX(A2,A3)^2']]},
        ]})
        wb = office_files.workbook(workbook.render(spec)[0])
        self.assertEqual(office_files.cell(wb, 'Calc', 'A2'), 23)
        self.assertEqual(office_files.cell(wb, 'Calc', 'A3'), 2.0)
        self.assertEqual(office_files.cell(wb, 'Calc', 'A4'), 529)

    def test_anything_outside_the_language_is_refused_not_run(self):
        wb = office_files.workbook(book([['North', 10]]))
        for formula in ('=__import__("os").getcwd()', '=A1.__class__',
                        '=POWER(A2,2)', '=[x for x in A2:A3]', '=A1()'):
            with self.subTest(formula=formula):
                with self.assertRaises(office_files.FormulaError):
                    office_files.evaluate(wb, 'Summary', formula)

    def test_the_grown_language_evaluates(self):
        # VLOOKUP and text used to be refused with the rest; they are part of
        # the language now (`inference/formulas.py`), so they evaluate.
        wb = office_files.workbook(book([['North', 10]]))
        self.assertEqual(office_files.evaluate(wb, 'Summary', '="text"'), 'text')
        self.assertEqual(
            office_files.evaluate(wb, 'Summary', '=VLOOKUP("North",A2:B2,2)'), 10)


class FileTypeTests(SimpleTestCase):
    def test_text_named_xlsx_is_not_a_workbook(self):
        g = grade({'type': 'file_type', 'path': 'b.xlsx', 'format': 'xlsx'}, **{'b.xlsx': b'Region,Revenue\n'})
        self.assertFalse(g.passed)

    def test_each_real_format_is_recognised(self):
        self.assertEqual(office_files.sniff(book([['a', 1]])), 'xlsx')
        d = deck.render(deck.validate({'slides': [{'layout': 'title', 'title': 't'}]}), {})
        self.assertEqual(office_files.sniff(d), 'pptx')
        w = document.render(document.validate({'title': 't', 'blocks': [{'type': 'paragraph', 'text': 'x'}]}), {})
        self.assertEqual(office_files.sniff(w), 'docx')


class PptxChartTests(SimpleTestCase):
    def deck(self, slide):
        return deck.render(deck.validate({'slides': [{'layout': 'title', 'title': 't'}, slide]}), {})

    def test_a_charted_series_passes_and_a_mentioned_one_does_not(self):
        charted = self.deck({'layout': 'chart', 'title': 'Revenue', 'chart': {
            'kind': 'column', 'title': 'Revenue', 'series': [
                {'name': 'Rev', 'points': [{'x': 'Q1', 'y': 3.1}, {'x': 'Q2', 'y': 3.6}]}]}})
        mentioned = self.deck({'layout': 'bullets', 'title': 'Revenue', 'bullets': ['Q1 3.1', 'Q2 3.6']})
        spec = {'type': 'pptx_chart', 'path': 'd.pptx', 'categories': ['Q1', 'Q2'], 'values': [3.1, 3.6]}
        self.assertTrue(grade(spec, **{'d.pptx': charted}).passed)
        self.assertFalse(grade(spec, **{'d.pptx': mentioned}).passed)

    def test_wrong_numbers_fail(self):
        charted = self.deck({'layout': 'chart', 'title': 'Revenue', 'chart': {
            'kind': 'column', 'title': 'Revenue', 'series': [
                {'name': 'Rev', 'points': [{'x': 'Q1', 'y': 3.1}, {'x': 'Q2', 'y': 9.9}]}]}})
        spec = {'type': 'pptx_chart', 'path': 'd.pptx', 'values': [3.1, 3.6]}
        self.assertFalse(grade(spec, **{'d.pptx': charted}).passed)

    def test_notes_count_as_slide_text(self):
        d = self.deck({'layout': 'bullets', 'title': 'x', 'bullets': ['y'], 'notes': 'festive season'})
        self.assertTrue(grade({'type': 'pptx_contains', 'path': 'd.pptx', 'value': 'festive'},
                              **{'d.pptx': d}).passed)


class DocxTests(SimpleTestCase):
    def test_headings_tables_and_text(self):
        data = document.render(document.validate({'title': 'Memo', 'blocks': [
            {'type': 'heading', 'text': 'Summary'},
            {'type': 'paragraph', 'text': 'Downtime: 47 minutes'},
            {'type': 'heading', 'text': 'Timeline'},
            {'type': 'table', 'columns': ['Time', 'Event'], 'rows': [['1', 'a'], ['2', 'b']]},
        ]}), {})
        files = {'m.docx': data}
        self.assertTrue(grade({'type': 'docx_headings', 'path': 'm.docx',
                               'includes': ['Summary', 'Timeline']}, **files).passed)
        self.assertFalse(grade({'type': 'docx_headings', 'path': 'm.docx',
                                'includes': ['Actions']}, **files).passed)
        self.assertTrue(grade({'type': 'docx_table', 'path': 'm.docx', 'min_rows': 2}, **files).passed)
        self.assertFalse(grade({'type': 'docx_table', 'path': 'm.docx', 'min_rows': 3}, **files).passed)
        self.assertTrue(grade({'type': 'docx_contains', 'path': 'm.docx',
                               'value': 'Downtime: 47 minutes'}, **files).passed)

    def test_a_missing_file_fails_rather_than_errors(self):
        g = grade({'type': 'docx_contains', 'path': 'nope.docx', 'value': 'x'})
        self.assertFalse(g.passed)
