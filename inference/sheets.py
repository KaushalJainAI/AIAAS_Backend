"""
Workbooks as Univer snapshots and back.

The Sheets app edits through Univer (`@univerjs/preset-sheets-core`); the
file on disk stays a real `.xlsx` that Excel opens. This module translates
between the two, using openpyxl on the **existing** workbook — like
`chat/tools/office/edit.py`, anything untouched (charts, images, print
settings, validations) survives the round trip, because openpyxl carries what
it does not understand through a load/save untouched.

The snapshot shape mirrors Univer 1.0.x `IWorkbookData` field for field
(`cellData`, `mergeData`, `columnData`/`rowData`, `freeze`, the `styles` map),
so the app passes it straight into `createUnit` and posts back what
`FWorkbook.save()` returns. Anything Univer-only (row counts, the workbook
id, selection state) is carried, never interpreted.

Two deliberate limits. Dates travel as Excel serial numbers with their number
format, because a Univer cell value is a string, number or boolean and has no
date — the format is what makes the number read as a date on both sides.
Sheet identity is the **name**: a snapshot sheet with an unknown name is
created, and when exactly one sheet was renamed (one unmatched name on each
side) it is renamed rather than duplicated.
"""
from __future__ import annotations

import datetime
import io
import uuid
from typing import Any

#: The Univer model version this snapshot targets (installed
#: `@univerjs/preset-sheets-core`). Bumped with the dependency, not by hand.
APP_VERSION = '1.0.2'

#: Rendering margin past the used range, so the grid does not end exactly
#: where the data does. openpyxl's `max_row`/`max_column` count formatted
#: cells too, so this is margin on top of an already generous edge.
_EXTRA_ROWS = 50
_EXTRA_COLS = 10


class SnapshotError(ValueError):
    """The snapshot is not one this module can apply."""


# ---------------------------------------------------------------------------
# xlsx bytes -> Univer snapshot
# ---------------------------------------------------------------------------

def to_snapshot(data: bytes) -> dict:
    """An `IWorkbookData`-shaped dict for the workbook in `data`."""
    import openpyxl

    try:
        book = openpyxl.load_workbook(io.BytesIO(data))
    except Exception as exc:  # noqa: BLE001 — a corrupt file is an answer, not a crash
        raise SnapshotError(f'That file could not be opened as a workbook ({exc}).') from exc
    try:
        styles: dict[str, dict] = {}
        style_ids: dict[str, str] = {}
        sheets: dict[str, dict] = {}
        order: list[str] = []
        for index, ws in enumerate(book.worksheets):
            sheet_id = f'sheet-{index}'
            order.append(sheet_id)
            sheets[sheet_id] = _worksheet(ws, sheet_id, styles, style_ids, book)
        return {
            'id': f'wb-{uuid.uuid4().hex[:8]}',
            'name': '',
            'appVersion': APP_VERSION,
            'locale': 'enUS',
            'styles': styles,
            'sheetOrder': order,
            'sheets': sheets,
        }
    finally:
        book.close()


def _worksheet(ws, sheet_id: str, styles: dict, style_ids: dict, book=None) -> dict:
    cell_data: dict[int, dict[int, dict]] = {}
    for row in ws.iter_rows():
        for cell in row:
            built = _cell_out(cell, styles, style_ids, book, ws.title)
            if built is not None:
                cell_data.setdefault(cell.row - 1, {})[cell.column - 1] = built
    out: dict[str, Any] = {
        'id': sheet_id,
        'name': ws.title,
        'rowCount': max((ws.max_row or 0) + _EXTRA_ROWS, 20),
        'columnCount': max((ws.max_column or 0) + _EXTRA_COLS, 8),
        'cellData': cell_data,
    }
    merges = [
        {'startRow': r.min_row - 1, 'endRow': r.max_row - 1,
         'startColumn': r.min_col - 1, 'endColumn': r.max_col - 1}
        for r in ws.merged_cells.ranges
    ]
    if merges:
        out['mergeData'] = merges
    columns = {}
    for letter, dim in ws.column_dimensions.items():
        if dim.width is not None:
            columns[_col_index(letter)] = {'w': round(dim.width * 7 + 5)}
    if columns:
        out['columnData'] = columns
    rows = {}
    for number, dim in ws.row_dimensions.items():
        if dim.height is not None:
            rows[number - 1] = {'h': round(dim.height * 96 / 72)}
    if rows:
        out['rowData'] = rows
    freeze = _freeze_out(ws.freeze_panes)
    if freeze is not None:
        out['freeze'] = freeze
    state = {'visible': 0, 'hidden': 1, 'veryHidden': 2}.get(ws.sheet_state, 0)
    if state:
        out['hidden'] = state
    tab = _color_hex(getattr(ws.sheet_properties, 'tabColor', None))
    if tab:
        out['tabColor'] = tab
    if ws.sheet_view.showGridLines is False:
        out['showGridlines'] = 0
    return out


def _cell_out(cell, styles: dict, style_ids: dict, book=None, sheet: str = '') -> dict | None:
    value = cell.value
    is_formula = cell.data_type == 'f' and isinstance(value, str) and value.startswith('=')
    if value is None and not is_formula:
        style_id = _style_id(cell, styles, style_ids)
        return {'s': style_id} if style_id is not None else None
    out: dict[str, Any] = {}
    if is_formula:
        out['f'] = value
        # The backend-calculated value rides along, so the grid shows a
        # number before the formula worker computes (and when it cannot).
        if book is not None:
            from . import formulas

            try:
                calculated = formulas.cell(book, sheet, cell.coordinate)
            except formulas.FormulaError:
                calculated = None
            if calculated is not None:
                out['v'] = _json_scalar(calculated)
    elif isinstance(value, bool):
        out['v'] = value
    elif isinstance(value, (int, float)):
        out['v'] = value
    elif isinstance(value, datetime.datetime):
        out['v'] = _to_serial(value)
    elif isinstance(value, datetime.date):
        out['v'] = _to_serial(value)
    else:
        out['v'] = str(value)
    style_id = _style_id(cell, styles, style_ids)
    if style_id is not None:
        out['s'] = style_id
    return out


def _to_serial(value) -> float:
    from openpyxl.utils.datetime import to_excel

    return float(to_excel(value))


def _json_scalar(value: Any) -> Any:
    """A calculated value the snapshot can carry: JSON scalars only."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (datetime.date, datetime.datetime)):
        return _to_serial(value)
    return str(value)


def _style_id(cell, styles: dict, style_ids: dict) -> str | None:
    style = _style_out(cell)
    if not style:
        return None
    import json

    key = json.dumps(style, sort_keys=True)
    if key not in style_ids:
        style_ids[key] = f'style-{len(style_ids) + 1}'
        styles[style_ids[key]] = style
    return style_ids[key]


def _style_out(cell) -> dict:
    out: dict[str, Any] = {}
    font = cell.font
    if font.bold:
        out['bl'] = 1
    if font.italic:
        out['it'] = 1
    # Defaults are omitted, not stored: an unstyled cell carries no style id,
    # which is also what keeps an emptied cell out of the snapshot.
    if font.size and font.size != 11:
        out['fs'] = font.size
    if font.name and str(font.name).lower() != 'calibri':
        out['ff'] = font.name
    color = _color_hex(font.color)
    if color:
        out['cl'] = {'rgb': color}
    if font.underline == 'single':
        out['ul'] = {'s': 1, 't': 12}
    if font.strike:
        out['st'] = {'s': 1}
    fill = cell.fill
    if getattr(fill, 'patternType', None) == 'solid':
        bg = _color_hex(getattr(fill, 'fgColor', None)) or _color_hex(getattr(fill, 'bgColor', None))
        if bg:
            out['bg'] = {'rgb': bg}
    alignment = cell.alignment
    horizontal = {'left': 1, 'center': 2, 'right': 3, 'justify': 4,
                  'distributed': 6, 'centerContinuous': 2}.get(alignment.horizontal or '')
    if horizontal:
        out['ht'] = horizontal
    vertical = {'top': 1, 'center': 2, 'bottom': 3}.get(alignment.vertical or '')
    if vertical:
        out['vt'] = vertical
    if alignment.wrap_text:
        out['tb'] = 3
    border = _border_out(cell.border)
    if border:
        out['bd'] = border
    if cell.number_format and cell.number_format != 'General':
        out['n'] = {'pattern': cell.number_format}
    return out


_BORDER_STYLES = {'thin': 1, 'hair': 2, 'dotted': 3, 'dashed': 4, 'dashDot': 5,
                  'dashDotDot': 6, 'double': 7, 'medium': 8, 'mediumDashed': 9,
                  'mediumDashDot': 10, 'mediumDashDotDot': 11, 'slantDashDot': 12,
                  'thick': 13}


def _border_out(border) -> dict:
    out = {}
    for side, key in ((border.top, 't'), (border.right, 'r'),
                      (border.bottom, 'b'), (border.left, 'l')):
        if side is None or not side.style:
            continue
        numbered = _BORDER_STYLES.get(side.style)
        if not numbered:
            continue
        entry: dict[str, Any] = {'s': numbered}
        color = _color_hex(side.color)
        if color:
            entry['cl'] = {'rgb': color}
        out[key] = entry
    return out


def _color_hex(color) -> str | None:
    """An openpyxl color as `#rrggbb`, or None when it is not a plain RGB."""
    if color is None:
        return None
    if getattr(color, 'type', None) != 'rgb':
        return None
    rgb = getattr(color, 'rgb', None)
    if not rgb or rgb == '00000000':
        return None
    return '#' + str(rgb)[-6:].lower()


def _freeze_out(ref: str | None) -> dict | None:
    """`'B2'` -> the Univer freeze for one row and one column."""
    if not ref or ref == 'A1':
        return None
    import re

    match = re.fullmatch(r'\$?([A-Z]{1,3})\$?(\d+)', ref or '')
    if not match:
        return None
    col = _col_index(match.group(1))
    row = int(match.group(2)) - 1
    if col <= 0 and row <= 0:
        return None
    return {'xSplit': col, 'ySplit': row, 'startRow': row, 'startColumn': col}


def _col_index(letter: str) -> int:
    from openpyxl.utils import column_index_from_string

    return column_index_from_string(letter) - 1


# ---------------------------------------------------------------------------
# Univer snapshot -> xlsx bytes (on the existing workbook)
# ---------------------------------------------------------------------------

def apply_snapshot(data: bytes, snapshot: dict) -> bytes:
    """Write a Univer snapshot back onto the workbook in `data`.

    Sheets match by name; cells, styles, merges, dimensions and freeze panes
    are replaced wholesale per sheet, and cells the snapshot no longer holds
    are cleared — an emptied cell that kept its value would be a deletion the
    file ignored. The snapshot is the whole workbook, so a worksheet it no
    longer holds was deleted in the app and is removed, and the tab order is
    the snapshot's. Everything else (charts, images, validations, print
    setup, chartsheets Univer never shows) is openpyxl's to carry through
    untouched.
    """
    import openpyxl

    if not isinstance(snapshot, dict) or not isinstance(snapshot.get('sheets'), dict):
        raise SnapshotError('Send the workbook as a snapshot with `sheets`.')
    try:
        book = openpyxl.load_workbook(io.BytesIO(data))
    except Exception as exc:  # noqa: BLE001 — a corrupt file is an answer, not a crash
        raise SnapshotError(f'That file could not be opened as a workbook ({exc}).') from exc
    try:
        incoming = list((snapshot.get('sheetOrder') or list(snapshot['sheets'])) or [])
        sheets = snapshot['sheets']
        ordered = [s for s in incoming if s in sheets] + [s for s in sheets if s not in incoming]
        if not ordered:
            raise SnapshotError('A workbook needs at least one sheet.')
        specs = [sheets[sid] for sid in ordered]
        matched = _match_sheets(book, specs)
        # `_match_sheets` pairs renames last; the tabs follow the snapshot.
        position = {id(spec): i for i, spec in enumerate(specs)}
        matched.sort(key=lambda pair: position.get(id(pair[1]), len(specs)))
        styles = snapshot.get('styles') or {}
        shared = _shared_formulas(matched)
        for ws, spec in matched:
            _apply_sheet(book, ws, spec, styles, shared)
        _prune_and_order(book, [ws for ws, _ in matched])
        buf = io.BytesIO()
        book.save(buf)
        return buf.getvalue()
    finally:
        book.close()


def unmerge_all(ws) -> list[tuple[int, int, int, int]]:
    """Remove every merge, instantiating missing cells first.

    `unmerge_cells` deletes each covered cell from the sheet and raises
    `KeyError` for cells that were never created (an empty merged area) — so
    create them first. Returns the bounds for re-merging.
    """
    bounds = [(r.min_col, r.min_row, r.max_col, r.max_row)
              for r in ws.merged_cells.ranges]
    for bound in list(ws.merged_cells.ranges):
        for r in range(bound.min_row, bound.max_row + 1):
            for c in range(bound.min_col, bound.max_col + 1):
                ws.cell(row=r, column=c)
        ws.unmerge_cells(str(bound))
    return bounds


def remerge(ws, bounds: list[tuple[int, int, int, int]]) -> None:
    """Re-merge bounds, dropping ones a structural edit collapsed."""
    for min_col, min_row, max_col, max_row in bounds:
        if min_row <= max_row and min_col <= max_col \
                and (min_row != max_row or min_col != max_col):
            ws.merge_cells(start_row=min_row, start_column=min_col,
                           end_row=max_row, end_column=max_col)


def _prune_and_order(book, kept: list) -> None:
    """Drop worksheets the snapshot no longer holds; order tabs as it does.

    Only worksheets: a chartsheet never reaches the app, so its absence from
    the snapshot says nothing, and it keeps its place after the worksheets.
    """
    from openpyxl.worksheet.worksheet import Worksheet

    was_active = book.active
    for ws in list(book.worksheets):
        if isinstance(ws, Worksheet) and all(ws is not k for k in kept):
            book.remove(ws)
    others = [ws for ws in book._sheets if all(ws is not k for k in kept)]
    book._sheets = list(kept) + others
    # The active tab follows its sheet, or falls back to the first.
    book.active = next((i for i, ws in enumerate(book._sheets) if ws is was_active), 0)


def _shared_formulas(matched: list[tuple]) -> dict:
    """Univer's shared-formula masters: `si` -> (formula, row, col), 1-based.

    A formula filled across cells is stored once: the first cell carries `f`
    and `si`, the rest only `si` (and a calculated `v`). Without resolving
    them every filled cell would be saved as its current value.
    """
    masters: dict = {}
    for _ws, spec in matched:
        for r_key, row in (spec.get('cellData') or {}).items():
            if not isinstance(row, dict):
                continue
            for c_key, cell in row.items():
                if not isinstance(cell, dict):
                    continue
                si, formula = cell.get('si'), cell.get('f')
                if si and isinstance(formula, str) and formula.startswith('=') and si not in masters:
                    try:
                        masters[si] = (formula, int(r_key) + 1, int(c_key) + 1)
                    except (TypeError, ValueError):
                        continue
    return masters


def _formula_of(cell: dict, r: int, c: int, shared: dict) -> str | None:
    """The cell's formula: its own `f`, or its shared master's, moved here."""
    formula = cell.get('f')
    if isinstance(formula, str) and formula.startswith('='):
        return formula
    master = shared.get(cell.get('si')) if cell.get('si') else None
    if master is None:
        return None
    from openpyxl.formula.translate import Translator
    from openpyxl.utils import get_column_letter

    text, mr, mc = master
    try:
        return Translator(text, origin=f'{get_column_letter(mc)}{mr}').translate_formula(
            f'{get_column_letter(c)}{r}')
    except Exception:  # noqa: BLE001 — an untranslatable formula keeps its value
        return None


def _match_sheets(book, specs: list) -> list[tuple]:
    """Pair snapshot sheets with workbook sheets, by name.

    An unknown name is created — except the rename case (one unmatched name
    on each side), which renames instead of leaving the old sheet behind next
    to an empty new one.
    """
    matched: list[tuple] = []
    unmatched_specs: list[dict] = []
    for spec in specs:
        name = spec.get('name') if isinstance(spec, dict) else None
        if isinstance(name, str) and name and name in book.sheetnames:
            matched.append((book[name], spec))
        else:
            unmatched_specs.append(spec if isinstance(spec, dict) else {})
    unmatched_sheets = [book[name] for name in book.sheetnames
                        if all(ws is not book[name] for ws, _ in matched)]
    if len(unmatched_specs) == 1 and len(unmatched_sheets) == 1:
        spec = unmatched_specs[0]
        ws = unmatched_sheets[0]
        if isinstance(spec.get('name'), str) and spec['name']:
            ws.title = spec['name']
        matched.append((ws, spec))
        unmatched_specs, unmatched_sheets = [], []
    for spec in unmatched_specs:
        name = spec.get('name') if isinstance(spec.get('name'), str) and spec.get('name') else 'Sheet'
        ws = book.create_sheet(title=name[:31])
        matched.append((ws, spec))
    return matched


def _apply_sheet(book, ws, spec: dict, styles: dict, shared: dict | None = None) -> None:
    from openpyxl.utils import get_column_letter

    cells = spec.get('cellData') or {}
    if not isinstance(cells, dict):
        raise SnapshotError(f'Sheet {spec.get("name")!r} has cellData that is not an object.')
    made = _style_maker(styles)

    # Unmerged while the cells land: a value written into a merged secondary
    # is read-only, and the snapshot's own merges go back on afterwards.
    unmerge_all(ws)

    seen: set[tuple[int, int]] = set()
    for r_key, row in cells.items():
        if not isinstance(row, dict):
            continue
        try:
            r = int(r_key) + 1
        except (TypeError, ValueError):
            continue
        for c_key, cell in row.items():
            if not isinstance(cell, dict):
                continue
            try:
                c = int(c_key) + 1
            except (TypeError, ValueError):
                continue
            seen.add((r, c))
            target = ws.cell(row=r, column=c)
            _apply_cell(target, cell, made, _formula_of(cell, r, c, shared or {}))

    # Cells the snapshot no longer holds were emptied in the app: clear them
    # within the file's used range, or a deletion is silently ignored.
    for row in ws.iter_rows(min_row=1, max_row=ws.max_row or 0,
                            max_col=ws.max_column or 0):
        for target in row:
            if (target.row, target.column) not in seen:
                _clear_cell(target)

    bounds = []
    for m in spec.get('mergeData') or []:
        try:
            bounds.append((int(m['startColumn']) + 1, int(m['startRow']) + 1,
                           int(m['endColumn']) + 1, int(m['endRow']) + 1))
        except (KeyError, TypeError, ValueError):
            raise SnapshotError(f'Sheet {spec.get("name")!r} has a merge that is not a range.')
    remerge(ws, bounds)

    for c_key, dim in (spec.get('columnData') or {}).items():
        try:
            letter = get_column_letter(int(c_key) + 1)
        except (TypeError, ValueError):
            continue
        if isinstance(dim, dict) and dim.get('w') is not None:
            try:
                ws.column_dimensions[letter].width = max(0.0, (float(dim['w']) - 5) / 7)
            except (TypeError, ValueError):
                pass
    for r_key, dim in (spec.get('rowData') or {}).items():
        try:
            number = int(r_key) + 1
        except (TypeError, ValueError):
            continue
        if isinstance(dim, dict) and dim.get('h') is not None:
            try:
                ws.row_dimensions[number].height = max(0.0, float(dim['h']) * 72 / 96)
            except (TypeError, ValueError):
                pass

    freeze = spec.get('freeze') or {}
    try:
        x, y = int(freeze.get('xSplit') or 0), int(freeze.get('ySplit') or 0)
    except (TypeError, ValueError):
        x = y = 0
    ws.freeze_panes = None if (x <= 0 and y <= 0) else f'{get_column_letter(x + 1)}{y + 1}'

    hidden = spec.get('hidden', 0)
    try:
        ws.sheet_state = {0: 'visible', 1: 'hidden', 2: 'veryHidden'}.get(int(hidden), 'visible')
    except (TypeError, ValueError):
        pass
    tab = spec.get('tabColor')
    if isinstance(tab, str) and tab.startswith('#') and len(tab) == 7:
        from openpyxl.styles import Color

        ws.sheet_properties.tabColor = Color(rgb='FF' + tab[1:].upper())
    grid = spec.get('showGridlines')
    if grid in (0, 1, True, False):
        ws.sheet_view.showGridLines = bool(grid)


def _apply_cell(target, cell: dict, made, formula: str | None = None) -> None:
    if formula is None:
        formula = cell.get('f')
    if isinstance(formula, str) and formula.startswith('='):
        target.value = formula
    elif 'v' in cell and cell['v'] is not None:
        target.value = cell['v']
    else:
        target.value = None
    style_id = cell.get('s')
    if style_id is None:
        _clear_style(target)
    else:
        font, fill, alignment, border, number_format = made(style_id)
        target.font, target.fill, target.alignment, target.border = font, fill, alignment, border
        target.number_format = number_format


def _clear_cell(target) -> None:
    target.value = None
    _clear_style(target)


def _clear_style(target) -> None:
    from openpyxl.styles import Alignment, Border, Font, PatternFill

    target.font = Font()
    target.fill = PatternFill()
    target.alignment = Alignment()
    target.border = Border()
    target.number_format = 'General'


def _style_maker(styles: dict):
    """Style ids of the snapshot -> openpyxl style tuples, built once."""
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side

    cache: dict[Any, tuple] = {}

    def make(style_id) -> tuple:
        if style_id in cache:
            return cache[style_id]
        spec = styles.get(style_id) if isinstance(styles, dict) else None
        if not isinstance(spec, dict):
            raise SnapshotError(f'Style {style_id!r} is not in the snapshot.')
        font = Font(bold=bool(spec.get('bl')), italic=bool(spec.get('it')),
                    size=spec.get('fs') or 11, name=spec.get('ff') or 'Calibri',
                    color=_ooxml_color((spec.get('cl') or {}).get('rgb')),
                    underline='single' if (spec.get('ul') or {}).get('s') else None,
                    strike=bool((spec.get('st') or {}).get('s')))
        bg = (spec.get('bg') or {}).get('rgb')
        fill = PatternFill(patternType='solid', fgColor=_ooxml_color(bg)) if bg else PatternFill()
        alignment = Alignment(
            horizontal={1: 'left', 2: 'center', 3: 'right', 4: 'justify'}.get(spec.get('ht') or 0),
            vertical={1: 'top', 2: 'center', 3: 'bottom'}.get(spec.get('vt') or 0),
            wrap_text=(spec.get('tb') or 0) == 3,
        )
        sides = {}
        for short, side in (('t', 'top'), ('r', 'right'),
                            ('b', 'bottom'), ('l', 'left')):
            entry = (spec.get('bd') or {}).get(short) or {}
            name = _BORDER_NAMES.get(entry.get('s'))
            if name is not None:
                sides[side] = Side(style=name,
                                   color=_ooxml_color((entry.get('cl') or {}).get('rgb')))
        border = Border(**sides)
        number_format = ((spec.get('n') or {}).get('pattern')) or 'General'
        cache[style_id] = (font, fill, alignment, border, number_format)
        return cache[style_id]

    return make


_BORDER_NAMES = {1: 'thin', 2: 'hair', 3: 'dotted', 4: 'dashed', 5: 'dashDot',
                 6: 'dashDotDot', 7: 'double', 8: 'medium', 9: 'mediumDashed',
                 10: 'mediumDashDot', 11: 'mediumDashDotDot', 12: 'slantDashDot',
                 13: 'thick'}


def _ooxml_color(rgb: str | None):
    from openpyxl.styles import Color

    if not isinstance(rgb, str):
        return None
    text = rgb.lstrip('#')
    if len(text) == 6 and all(c in '0123456789abcdefABCDEF' for c in text):
        return Color(rgb='FF' + text.upper())
    return None
