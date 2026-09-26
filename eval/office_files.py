"""
Reading rendered office files back, for the graders that judge them.

A work-tier case is graded on the files an agent produced (`eval/workspace.py`),
and for a deck, a workbook or a Word document the text extract is not enough:
"has a chart", "the Total row is a formula" and "the Q3 bar is 4.2" are facts
about the file's *structure*, which only its real reader can see. So these
open the bytes with python-pptx, openpyxl and python-docx — the same libraries
the office tools render with, and the ones a user's Office would agree with.

**Formulas are evaluated, not trusted and not skipped.** xlsxwriter writes
formulas without cached values, so a grader reading `data_only` values sees
nothing, and one reading the formula text learns only that *some* formula is
there. `evaluate` (re-exported from `office/formulas.py`, which grew from
what was here) computes the common subset, and anything outside it is reported
as not evaluable rather than guessed. It evaluates a model-written string
inside this process, so the expression is parsed to an AST and every node is
checked against a closed list before anything runs; there are no builtins in
its namespace.
"""
from __future__ import annotations

import io
import zipfile
from typing import Any

#: Part that identifies each format inside its zip container.
_SIGNATURES = {
    'pptx': 'ppt/presentation.xml',
    'xlsx': 'xl/workbook.xml',
    'docx': 'word/document.xml',
}


def sniff(data: bytes) -> str | None:
    """What the bytes really are — not what the file was named."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            names = set(zf.namelist())
    except zipfile.BadZipFile:
        return None
    for kind, part in _SIGNATURES.items():
        if part in names:
            return kind
    return None


# ---------------------------------------------------------------- pptx

def _presentation(data: bytes):
    from pptx import Presentation

    return Presentation(io.BytesIO(data))


def pptx_slide_count(data: bytes) -> int:
    return len(_presentation(data).slides)


def pptx_text(data: bytes) -> str:
    """Every word on every slide — text boxes, tables, and speaker notes."""
    out: list[str] = []
    for slide in _presentation(data).slides:
        for shape in slide.shapes:
            if shape.has_text_frame:
                out.append(shape.text_frame.text)
            if getattr(shape, 'has_table', False) and shape.has_table:
                for row in shape.table.rows:
                    out.append(' | '.join(cell.text for cell in row.cells))
            if getattr(shape, 'has_chart', False) and shape.has_chart:
                chart = shape.chart
                if chart.has_title:
                    out.append(chart.chart_title.text_frame.text)
        if slide.has_notes_slide:
            out.append(slide.notes_slide.notes_text_frame.text)
    return '\n'.join(out)


def pptx_charts(data: bytes) -> list[dict[str, Any]]:
    """Every native chart: its categories and each series' values."""
    charts = []
    for slide in _presentation(data).slides:
        for shape in slide.shapes:
            if not (getattr(shape, 'has_chart', False) and shape.has_chart):
                continue
            plot = shape.chart.plots[0]
            try:
                categories = [str(c) for c in plot.categories]
            except Exception:  # noqa: BLE001 — XY charts have no categories
                categories = []
            charts.append({
                'categories': categories,
                'series': {s.name: list(s.values) for s in plot.series},
            })
    return charts


# ---------------------------------------------------------------- docx

def _document(data: bytes):
    import docx

    return docx.Document(io.BytesIO(data))


def docx_text(data: bytes) -> str:
    d = _document(data)
    parts = [p.text for p in d.paragraphs]
    for table in d.tables:
        for row in table.rows:
            parts.append(' | '.join(c.text for c in row.cells))
    return '\n'.join(parts)


def docx_headings(data: bytes) -> list[str]:
    """Text of every paragraph styled as a heading (or the title)."""
    return [p.text.strip() for p in _document(data).paragraphs
            if p.style is not None and (p.style.name.startswith('Heading') or p.style.name == 'Title')
            and p.text.strip()]


def docx_table_rows(data: bytes) -> list[int]:
    """Data rows (header excluded) in each table, in order."""
    return [max(0, len(t.rows) - 1) for t in _document(data).tables]


# ---------------------------------------------------------------- xlsx
#
# The evaluator lives in `office/formulas.py` — grown from what was here,
# so the benchmark grades with the same values the app shows. It is re-exported
# lazily (module `__getattr__`, below) rather than imported at module scope:
# nothing in `eval/` imports a sibling app at module scope, so no cycle is
# possible. `find_row` / `column_letter` stay local: they need no evaluator.

_EVALUATOR_NAMES = frozenset({
    'FormulaError', 'workbook', 'has_chart', 'cell', 'evaluate',
})


def __getattr__(name: str):
    if name in _EVALUATOR_NAMES:
        from office import formulas

        return getattr(formulas, name)
    raise AttributeError(f'module {__name__!r} has no attribute {name!r}')


def find_row(wb, sheet: str, match: dict[str, Any]) -> int | None:
    """The sheet row (1-based) whose header-named cells equal `match`."""
    ws = wb[sheet]
    headers = [str(c.value).strip() if c.value is not None else '' for c in ws[1]]
    try:
        cols = {k: headers.index(k) for k in match}
    except ValueError:
        return None
    for r, row in enumerate(ws.iter_rows(min_row=2), start=2):
        if all(str(row[i].value).strip().lower() == str(match[k]).strip().lower()
               for k, i in cols.items()):
            return r
    return None


def column_letter(wb, sheet: str, header: str) -> str | None:
    from openpyxl.utils import get_column_letter

    for i, c in enumerate(wb[sheet][1], start=1):
        if c.value is not None and str(c.value).strip() == header:
            return get_column_letter(i)
    return None
