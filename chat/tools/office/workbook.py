"""
`.xlsx` from a spec: sheets, typed columns, rows, live formulas, native charts.

What makes this an Excel agent rather than a CSV export is that **formulas stay
formulas**. A total the model computed and typed in is a number that goes
stale the moment someone edits a row; `=SUM(B2:B9)` is still right after they
do. So a string starting with `=` is written as a formula, `{r}` inside one is
the row it sits on (so a per-row formula is one string, not N hand-numbered
ones), and the totals row is `SUM` over the column rather than a figure.

Everything visual is decided here — header style, number formats by column
type, widths, frozen header, filter, chart colours — so the model describes
the data and never the formatting.

xlsxwriter writes formulas without cached results and marks the workbook for
full recalculation on load, so Excel, LibreOffice and Google Sheets all show
computed values on open.
"""
from __future__ import annotations

import io
import re
from datetime import date
from typing import Any

import xlsxwriter

from workflow_backend.thresholds import (
    CHART_MAX_POINTS_PER_SERIES,
    CHART_MAX_SERIES,
    CHART_MAX_SERIES_ALL_PAIRS,
)

from ..charts import KINDS as CHART_KINDS
from .spec import SpecError, choice, items, text
from .themes import LIGHT_PALETTE

MAX_SHEETS = 10
MAX_COLUMNS = 50
MAX_ROWS = 5000
#: Workspace knobs (`render_workbook.maxRows/maxSheets`); the constants stay
#: as the floor under a failed overlay read — validation takes the knob and
#: clamps it to never exceed these, so a stored value cannot widen past the
#: code ceiling.
MAX_CELL_CHARS = 2000
MAX_HEADER_CHARS = 80
#: Rows per sheet kept in the stored spec for the in-app preview. The file has
#: all of them; the preview says how many it is not showing.
PREVIEW_ROWS = 100
#: Rows per sheet put into the searchable text extract.
TEXT_ROWS = 200

COLUMN_TYPES = ('text', 'number', 'integer', 'currency', 'percent', 'date')
NUMERIC_TYPES = frozenset({'number', 'integer', 'currency', 'percent'})
#: Summed by `totals: true`. A column of percentages does not add up to
#: anything, so it is left out rather than totalled to 340%.
SUMMABLE_TYPES = frozenset({'number', 'integer', 'currency'})

CURRENCY_SYMBOLS = {'INR': '₹', 'USD': '$', 'EUR': '€', 'GBP': '£', 'JPY': '¥'}

_SHEET_ILLEGAL = re.compile(r'[\[\]:*?/\\]')
_ISO_DATE = re.compile(r'^\d{4}-\d{2}-\d{2}$')

#: Functions that reach outside the workbook, and DDE's `app|topic` form. A
#: model that read a hostile page can be talked into writing one; a workbook
#: that phones home when the user opens it is not a spreadsheet they asked for.
_UNSAFE_FORMULA = re.compile(
    r'\b(WEBSERVICE|FILTERXML|CALL|REGISTER(?:\.ID)?|EXEC|RTD|IMPORTXML|IMPORTDATA|'
    r'IMPORTHTML|IMPORTFEED|IMPORTRANGE|IMAGE|HYPERLINK)\s*\(|\|',
    re.IGNORECASE,
)

HEADER_FILL = '#E8F0FB'
HEADER_BORDER = '#C9D6EA'
GRID = '#D9DDE3'


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def validate(args: dict, *, max_sheets: int = MAX_SHEETS,
               max_rows: int = MAX_ROWS) -> dict:
    """The normalised spec, or `SpecError` saying what to fix.

    `max_sheets` / `max_rows` are the caller's workspace knobs
    (`render_workbook.maxSheets/maxRows`) resolved by the tool layer, clamped
    to never exceed the module ceilings — a stored knob narrows, never widens.
    """
    sheets_raw = items(args.get('sheets'), 'sheets',
                       max(1, min(max_sheets, MAX_SHEETS)), required=True)
    sheets: list[dict] = []
    seen: set[str] = set()
    for i, raw in enumerate(sheets_raw, 1):
        if not isinstance(raw, dict):
            raise SpecError(f'Sheet {i} must be an object with name, columns and rows.')
        name = _SHEET_ILLEGAL.sub('-', text(raw.get('name') or f'Sheet{i}', f'Sheet {i} name', 31))
        if name.lower() in seen:
            raise SpecError(f'Two sheets are called "{name}". Sheet names must differ.')
        seen.add(name.lower())

        columns = _columns(raw.get('columns'), name)
        headers = [c['header'] for c in columns]
        rows = _rows(raw.get('rows'), name, headers,
                     max_rows=max(1, min(max_rows, MAX_ROWS)))
        sheets.append({
            'name': name,
            'columns': columns,
            'rows': rows,
            'totals': _totals(raw.get('totals'), name, columns),
            'chart': _chart(raw.get('chart'), name, columns, len(rows)),
        })
    return {'sheets': sheets}


def _columns(raw: Any, sheet: str) -> list[dict]:
    out: list[dict] = []
    for j, col in enumerate(items(raw, f'{sheet}: columns', MAX_COLUMNS, required=True), 1):
        if isinstance(col, str):
            col = {'header': col}
        if not isinstance(col, dict):
            raise SpecError(f'{sheet}: column {j} must be a header string or an object.')
        header = text(col.get('header'), f'{sheet}: column {j} header', MAX_HEADER_CHARS,
                      required=True)
        ctype = choice(col.get('type'), f'{sheet}: column "{header}" type', COLUMN_TYPES, 'text')
        entry = {'header': header, 'type': ctype}
        if ctype == 'currency':
            code = str(col.get('currency') or '').strip().upper()
            if code and code not in CURRENCY_SYMBOLS:
                raise SpecError(
                    f'{sheet}: currency {code!r} is not supported. Use one of '
                    f'{", ".join(CURRENCY_SYMBOLS)}, or type "number".'
                )
            if code:
                entry['currency'] = code
        out.append(entry)
    headers = [c['header'].lower() for c in out]
    if len(set(headers)) != len(headers):
        raise SpecError(f'{sheet}: two columns share a header. Headers must differ.')
    return out


def _rows(raw: Any, sheet: str, headers: list[str],
            max_rows: int = MAX_ROWS) -> list[list]:
    out: list[list] = []
    for r, row in enumerate(items(raw, f'{sheet}: rows', max_rows), 1):
        if isinstance(row, dict):
            # Keyed by header: the shape a model most often has the data in
            # already, and one where a missing key is plainly a blank cell.
            unknown = [k for k in row if k not in headers]
            if unknown:
                raise SpecError(f'{sheet}: row {r} names columns that do not exist: {unknown[:3]}.')
            row = [row.get(h) for h in headers]
        if not isinstance(row, list):
            raise SpecError(f'{sheet}: row {r} must be a list of values (or an object keyed by header).')
        if len(row) > len(headers):
            raise SpecError(
                f'{sheet}: row {r} has {len(row)} values but there are '
                f'{len(headers)} columns.'
            )
        cells = []
        for v in row:
            if v is not None and not isinstance(v, (str, int, float, bool)):
                raise SpecError(f'{sheet}: row {r} has a value that is not text or a number.')
            if isinstance(v, str):
                if len(v) > MAX_CELL_CHARS:
                    raise SpecError(
                        f'{sheet}: a cell in row {r} is {len(v)} characters; the '
                        f'limit is {MAX_CELL_CHARS}.'
                    )
                if v.startswith('='):
                    if _UNSAFE_FORMULA.search(v):
                        raise SpecError(
                            f'{sheet}: row {r} has a formula that reaches outside the '
                            f'workbook ({v[:40]}). Only formulas over the workbook\'s own '
                            f'cells are allowed.'
                        )
                    # Resolved here, not at write time, so the stored preview
                    # and the search text show the formula the file holds.
                    # Row 1 is the header, so data row r sits on sheet row r+1.
                    v = v.replace('{r}', str(r + 1))
            cells.append(v)
        cells += [None] * (len(headers) - len(cells))
        out.append(cells)
    return out


def _totals(raw: Any, sheet: str, columns: list[dict]) -> list[str]:
    if raw is True:
        return [c['header'] for c in columns if c['type'] in SUMMABLE_TYPES]
    if not raw:
        return []
    by_header = {c['header']: c for c in columns}
    wanted = items(raw, f'{sheet}: totals', MAX_COLUMNS)
    for h in wanted:
        col = by_header.get(h)
        if col is None:
            raise SpecError(f'{sheet}: totals names "{h}", which is not a column.')
        if col['type'] not in SUMMABLE_TYPES:
            raise SpecError(
                f'{sheet}: "{h}" is a {col["type"]} column and cannot be totalled. '
                f'Give it type number, integer or currency.'
            )
    return list(wanted)


def _chart(raw: Any, sheet: str, columns: list[dict], n_rows: int) -> dict | None:
    if not raw:
        return None
    if not isinstance(raw, dict):
        raise SpecError(f'{sheet}: chart must be an object.')
    where = f'{sheet}: chart'
    kind = choice(raw.get('kind'), f'{where} kind', CHART_KINDS, '')
    title = text(raw.get('title'), f'{where} title', 120, required=True)
    by_header = {c['header']: c for c in columns}

    x = text(raw.get('x'), f'{where} x', MAX_HEADER_CHARS, required=True)
    if x not in by_header:
        raise SpecError(f'{where}: x names "{x}", which is not a column.')
    ys = raw.get('y')
    if isinstance(ys, str):
        ys = [ys]
    ys = items(ys, f'{where} y', CHART_MAX_SERIES, required=True)
    for y in ys:
        col = by_header.get(y)
        if col is None:
            raise SpecError(f'{where}: y names "{y}", which is not a column.')
        if col['type'] not in NUMERIC_TYPES:
            raise SpecError(f'{where}: "{y}" is a {col["type"]} column; a chart needs numbers.')
    if kind == 'pie' and len(ys) != 1:
        raise SpecError(f'{where}: a pie shows one series. Give exactly one y column.')
    if kind == 'pie' and n_rows > len(LIGHT_PALETTE):
        raise SpecError(
            f'{where}: a pie with {n_rows} slices cannot be told apart; the limit '
            f'is {len(LIGHT_PALETTE)}. Use a bar chart.'
        )
    if kind == 'scatter' and len(ys) > CHART_MAX_SERIES_ALL_PAIRS:
        raise SpecError(f'{where}: scatter takes at most {CHART_MAX_SERIES_ALL_PAIRS} y columns.')
    if n_rows == 0:
        raise SpecError(f'{where}: the sheet has no rows to chart.')
    if n_rows > CHART_MAX_POINTS_PER_SERIES:
        raise SpecError(
            f'{where}: {n_rows} rows is more points than a chart can show '
            f'(limit {CHART_MAX_POINTS_PER_SERIES}). Chart a summary sheet instead.'
        )
    return {
        'kind': kind, 'title': title, 'x': x, 'y': list(ys),
        'x_label': text(raw.get('x_label'), f'{where} x_label', 80),
        'y_label': text(raw.get('y_label'), f'{where} y_label', 80),
        'stacked': bool(raw.get('stacked')) and kind in ('bar', 'column', 'area'),
    }


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def render(spec: dict) -> tuple[bytes, list[str]]:
    """The workbook's bytes, and warnings about values written as text."""
    buf = io.BytesIO()
    wb = xlsxwriter.Workbook(buf, {
        'in_memory': True,
        # Values are typed by their column, never guessed from their text.
        'strings_to_numbers': False,
        'strings_to_formulas': False,
        'strings_to_urls': False,
    })
    warnings: list[str] = []
    fmt = _Formats(wb)

    for sheet in spec['sheets']:
        ws = wb.add_worksheet(sheet['name'])
        columns, rows = sheet['columns'], sheet['rows']
        ws.write_row(0, 0, [c['header'] for c in columns], fmt.header)
        ws.set_row(0, 20)

        widths = [max(len(c['header']), 6) for c in columns]
        for r, row in enumerate(rows, start=1):
            for c, value in enumerate(row):
                shown = _write(ws, r, c, value, columns[c], fmt, warnings, sheet['name'])
                widths[c] = max(widths[c], min(shown, 60))

        if sheet['totals'] and rows:
            _write_totals(ws, sheet, fmt)

        for c, width in enumerate(widths):
            ws.set_column(c, c, width + 2)
        ws.freeze_panes(1, 0)
        if rows:
            ws.autofilter(0, 0, len(rows), len(columns) - 1)
        if sheet['chart']:
            _insert_chart(wb, ws, sheet)

    wb.close()
    # One warning per sheet+column is enough to act on; forty identical ones
    # push the useful part of the result off the model's screen.
    return buf.getvalue(), list(dict.fromkeys(warnings))[:10]


class _Formats:
    def __init__(self, wb):
        self.header = wb.add_format({
            'bold': True, 'bg_color': HEADER_FILL, 'bottom': 1,
            'bottom_color': HEADER_BORDER, 'valign': 'vcenter',
        })
        self._wb = wb
        self._cache: dict[tuple, Any] = {}

    def for_column(self, column: dict, *, total: bool = False):
        key = (column['type'], column.get('currency'), total)
        if key not in self._cache:
            props: dict[str, Any] = {}
            number = _number_format(column)
            if number:
                props['num_format'] = number
            if total:
                props.update({'bold': True, 'top': 1, 'top_color': HEADER_BORDER})
            if column['type'] == 'text':
                props['text_wrap'] = False
            self._cache[key] = self._wb.add_format(props)
        return self._cache[key]


def _number_format(column: dict) -> str:
    kind = column['type']
    if kind == 'integer':
        return '#,##0'
    if kind == 'number':
        return '#,##0.00'
    if kind == 'percent':
        return '0.0%'
    if kind == 'date':
        return 'yyyy-mm-dd'
    if kind == 'currency':
        symbol = CURRENCY_SYMBOLS.get(column.get('currency', ''), '')
        return f'"{symbol}"#,##0.00' if symbol else '#,##0.00'
    return ''


def _write(ws, r: int, c: int, value: Any, column: dict, fmt: _Formats,
           warnings: list[str], sheet: str) -> int:
    """Write one cell by its column's type. Returns its display width."""
    cell_fmt = fmt.for_column(column)
    if value is None or value == '':
        return 0
    if isinstance(value, str) and value.startswith('='):
        ws.write_formula(r, c, value, cell_fmt)
        return 12
    if isinstance(value, bool):
        ws.write_boolean(r, c, value, cell_fmt)
        return 5

    kind = column['type']
    if kind in NUMERIC_TYPES:
        number = _as_number(value, percent=kind == 'percent')
        if number is None:
            warnings.append(
                f'{sheet}: "{column["header"]}" is a {kind} column but some '
                f'values were not numbers, so they were written as text.'
            )
            ws.write_string(r, c, str(value), cell_fmt)
            return len(str(value))
        ws.write_number(r, c, number, cell_fmt)
        return len(f'{number:,.2f}') + 2
    if kind == 'date' and isinstance(value, str) and _ISO_DATE.match(value.strip()):
        try:
            ws.write_datetime(r, c, _date(value.strip()), cell_fmt)
            return 10
        except ValueError:
            pass
    if isinstance(value, (int, float)):
        ws.write_number(r, c, value, cell_fmt)
        return len(str(value))
    ws.write_string(r, c, str(value), cell_fmt)
    return len(str(value))


def _date(value: str):
    from datetime import datetime

    parsed = date.fromisoformat(value)
    return datetime(parsed.year, parsed.month, parsed.day)


def _as_number(value: Any, *, percent: bool) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    raw = str(value).strip()
    is_percent = raw.endswith('%')
    raw = raw.rstrip('%').replace(',', '').strip()
    for symbol in CURRENCY_SYMBOLS.values():
        raw = raw.replace(symbol, '')
    try:
        number = float(raw)
    except ValueError:
        return None
    if number != number or number in (float('inf'), float('-inf')):
        return None
    return number / 100 if (percent and is_percent) else number


def _write_totals(ws, sheet: dict, fmt: _Formats) -> None:
    from xlsxwriter.utility import xl_col_to_name

    columns, n = sheet['columns'], len(sheet['rows'])
    row = n + 1
    totalled = set(sheet['totals'])
    first = columns[0]['header']
    if first not in totalled:
        ws.write_string(row, 0, 'Total', fmt.for_column({'type': 'text'}, total=True))
    for c, column in enumerate(columns):
        if column['header'] in totalled:
            letter = xl_col_to_name(c)
            ws.write_formula(row, c, f'=SUM({letter}2:{letter}{n + 1})',
                             fmt.for_column(column, total=True))


def _insert_chart(wb, ws, sheet: dict) -> None:
    spec = sheet['chart']
    headers = [c['header'] for c in sheet['columns']]
    n = len(sheet['rows'])
    kind = spec['kind']
    options: dict[str, Any] = {'type': kind}
    if spec['stacked']:
        options['subtype'] = 'stacked'
    if kind == 'line':
        options['type'] = 'line'
    chart = wb.add_chart(options)

    xc = headers.index(spec['x'])
    name = sheet['name']
    for k, header in enumerate(spec['y']):
        yc = headers.index(header)
        color = '#' + LIGHT_PALETTE[k % len(LIGHT_PALETTE)]
        series: dict[str, Any] = {
            'name': [name, 0, yc],
            'categories': [name, 1, xc, n, xc],
            'values': [name, 1, yc, n, yc],
        }
        if kind == 'pie':
            series['points'] = [
                {'fill': {'color': '#' + LIGHT_PALETTE[i % len(LIGHT_PALETTE)]}}
                for i in range(n)
            ]
        elif kind in ('line', 'scatter'):
            series['marker'] = {'type': 'circle', 'size': 5,
                                'fill': {'color': color}, 'border': {'color': color}}
            if kind == 'line':
                series['line'] = {'color': color, 'width': 2.25}
        else:
            series['fill'] = {'color': color}
            series['border'] = {'none': True}
        chart.add_series(series)

    chart.set_title({'name': spec['title'], 'name_font': {'size': 13, 'bold': True}})
    if kind != 'pie':
        chart.set_x_axis({'name': spec['x_label'] or spec['x'],
                          'line': {'color': GRID}})
        chart.set_y_axis({'name': spec['y_label'] or '',
                          'major_gridlines': {'visible': True, 'line': {'color': GRID}},
                          'line': {'none': True}})
    if kind != 'pie' and len(spec['y']) == 1:
        chart.set_legend({'none': True})
    else:
        chart.set_legend({'position': 'bottom'})
    chart.set_size({'width': 640, 'height': 360})
    ws.insert_chart(1, len(headers) + 1, chart)


# ---------------------------------------------------------------------------
# What is stored besides the bytes
# ---------------------------------------------------------------------------

def preview(spec: dict) -> dict:
    """The spec trimmed to what the in-app preview draws."""
    return {
        'kind': 'workbook',
        'sheets': [
            {
                'name': s['name'],
                'columns': s['columns'],
                'rows': s['rows'][:PREVIEW_ROWS],
                'row_count': len(s['rows']),
                'totals': s['totals'],
                'chart': s['chart'],
            }
            for s in spec['sheets']
        ],
    }


def extract_text(spec: dict) -> str:
    """Searchable text: every sheet's headers and its first rows."""
    out: list[str] = []
    for s in spec['sheets']:
        out.append(f'# Sheet: {s["name"]} ({len(s["rows"])} rows)')
        out.append(' | '.join(c['header'] for c in s['columns']))
        for row in s['rows'][:TEXT_ROWS]:
            out.append(' | '.join('' if v is None else str(v) for v in row))
        if len(s['rows']) > TEXT_ROWS:
            out.append(f'... {len(s["rows"]) - TEXT_ROWS} more rows')
        if s['chart']:
            out.append(f'Chart: {s["chart"]["title"]}')
        out.append('')
    return '\n'.join(out)
