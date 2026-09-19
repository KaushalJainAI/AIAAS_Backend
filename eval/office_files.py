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
there. `evaluate` computes the common subset — cell and range references (also
across sheets), `+ - * / ^`, and SUM / AVERAGE / MIN / MAX / COUNT / ROUND /
ABS — and anything outside it is reported as not evaluable rather than
guessed. It evaluates a model-written string inside this process, so the
expression is parsed to an AST and every node is checked against a closed list
before anything runs; there are no builtins in its namespace.
"""
from __future__ import annotations

import ast
import io
import re
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

class FormulaError(ValueError):
    """The formula uses something outside the evaluable subset."""


def workbook(data: bytes):
    import openpyxl

    return openpyxl.load_workbook(io.BytesIO(data))  # formulas, not cached values


def has_chart(wb, sheet: str | None = None) -> bool:
    sheets = [wb[sheet]] if sheet else wb.worksheets
    return any(getattr(ws, '_charts', None) for ws in sheets)


_SHEET = r"(?:'(?P<qs>[^']+)'|(?P<s>[A-Za-z_][\w.]*))!"
_CELL = r'\$?[A-Z]{1,3}\$?\d+'
#: Both anchored on the left, so `LOG10(` is not read as a reference to `OG10`
#: and `Data!A1` is not read a second time as a bare `A1`.
_RANGE_RE = re.compile(rf"(?<![\w.!']){'(?:' + _SHEET + ')'}?(?P<a>{_CELL}):(?P<b>{_CELL})")
_REF_RE = re.compile(rf"(?<![\w.!']){'(?:' + _SHEET + ')'}?(?P<a>{_CELL})(?![\w(])")
_FUNCS = {'SUM', 'AVERAGE', 'MIN', 'MAX', 'COUNT', 'ROUND', 'ABS'}
_ALLOWED_NODES = (ast.Expression, ast.BinOp, ast.UnaryOp, ast.Call, ast.Name, ast.Load,
                  ast.Constant, ast.Add, ast.Sub, ast.Mult, ast.Div, ast.Pow, ast.USub,
                  ast.UAdd, ast.List)


def cell(wb, sheet: str, ref: str, _depth: int = 0) -> Any:
    """A cell's value, evaluating it when it holds a formula."""
    value = wb[sheet][ref.replace('$', '')].value
    if isinstance(value, str) and value.startswith('='):
        return evaluate(wb, sheet, value, _depth + 1)
    return value


def evaluate(wb, sheet: str, formula: str, _depth: int = 0) -> float:
    if _depth > 30:
        raise FormulaError('formulas refer to each other too deeply (a cycle?)')
    body = formula.lstrip('=').strip()
    refs: list[tuple[str, str, str | None]] = []

    def hold(sheet_name: str, a: str, b: str | None) -> str:
        refs.append((sheet_name, a, b))
        return f'__r{len(refs) - 1}'

    def _range(m):
        return hold(m.group('qs') or m.group('s') or sheet, m.group('a'), m.group('b'))

    def _ref(m):
        return hold(m.group('qs') or m.group('s') or sheet, m.group('a'), None)

    body = _RANGE_RE.sub(_range, body)
    body = _REF_RE.sub(_ref, body)
    body = body.replace('^', '**')
    try:
        tree = ast.parse(body, mode='eval')
    except SyntaxError as exc:
        raise FormulaError(f'cannot parse {formula!r}') from exc
    for node in ast.walk(tree):
        if not isinstance(node, _ALLOWED_NODES):
            raise FormulaError(f'{formula!r} uses {type(node).__name__}, which is not evaluable here')
        if isinstance(node, ast.Name) and not (node.id in _FUNCS or node.id.startswith('__r')):
            raise FormulaError(f'{formula!r} uses {node.id}, which is not evaluable here')
        if isinstance(node, ast.Call) and not (isinstance(node.func, ast.Name) and node.func.id in _FUNCS):
            raise FormulaError(f'{formula!r} calls something outside {sorted(_FUNCS)}')
        if isinstance(node, ast.Constant) and not isinstance(node.value, (int, float)):
            raise FormulaError(f'{formula!r} has a non-numeric constant')

    names: dict[str, Any] = {}
    for i, (sheet_name, a, b) in enumerate(refs):
        if sheet_name not in wb.sheetnames:
            raise FormulaError(f'{formula!r} refers to a sheet that does not exist: {sheet_name}')
        if b is None:
            names[f'__r{i}'] = _num(cell(wb, sheet_name, a, _depth))
        else:
            ws = wb[sheet_name]
            names[f'__r{i}'] = [_num(cell(wb, sheet_name, c.coordinate, _depth))
                                for row in ws[a.replace('$', ''):b.replace('$', '')] for c in row]
    names.update(_functions())
    return float(eval(compile(tree, '<formula>', 'eval'), {'__builtins__': {}}, names))  # noqa: S307 — AST-checked above


def _num(value: Any) -> float | None:
    if value is None or value == '':
        return None
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).replace(',', ''))
    except ValueError:
        return None


def _flat(args) -> list[float]:
    out: list[float] = []
    for a in args:
        if isinstance(a, list):
            out.extend(v for v in a if v is not None)
        elif a is not None:
            out.append(a)
    return out


def _functions() -> dict[str, Any]:
    def average(*a):
        vals = _flat(a)
        if not vals:
            raise FormulaError('AVERAGE of no numbers')
        return sum(vals) / len(vals)

    return {
        'SUM': lambda *a: sum(_flat(a)),
        'AVERAGE': average,
        'MIN': lambda *a: min(_flat(a)),
        'MAX': lambda *a: max(_flat(a)),
        'COUNT': lambda *a: float(len(_flat(a))),
        'ROUND': lambda x, n=0: round(x, int(n)),
        'ABS': abs,
    }


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
