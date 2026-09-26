"""
Editing a workbook that already exists, instead of re-rendering it.

`render_workbook` replaces a file wholesale, which is the right shape for
"build me this workbook" and the wrong one for "add yesterday's numbers to the
tracker": re-rendering means the model re-emitting every row it did not touch,
billed twice and silently paraphrased on the way through — the same argument
`edit_file` makes against `write_file`, one format up.

So this appends rows and sets cells, through openpyxl, keeping everything it
does not name: formatting, other sheets, charts, column widths, and every
formula it is not overwriting.

Structural edits (inserting or deleting rows and columns mid-sheet) shift
formula references in every sheet and move merges along, so a row added above
a total does not silently break it. Formatting, freeze panes and column widths
are operations of their own rather than side effects of writing values.

Refusals are the point, as everywhere in `office/`:

* a sheet that does not exist is an error naming the sheets that do — creating
  it silently is how an append lands somewhere nobody looks;
* a cell reference that is not a reference is refused rather than guessed;
* the row and cell caps are the same ones `render_workbook` enforces, so a
  file cannot grow past them by being edited instead of written.
"""
from __future__ import annotations

import io
import re
from typing import Any

from .spec import SpecError, items, text
from .workbook import MAX_CELL_CHARS, MAX_COLUMNS, MAX_ROWS, _UNSAFE_FORMULA

CELL_REF = re.compile(r'^\$?[A-Z]{1,3}\$?[1-9][0-9]{0,6}$')
#: Workspace knob (`edit_workbook.maxEdits`); the constant stays as the floor
#: under a failed overlay read — validation clamps the knob to never exceed it.
MAX_EDITS = 200

_ALIGNMENTS = {'left', 'center', 'right', 'justify', 'distributed',
              'top', 'bottom', 'middle'}
_BORDER_NAMED = {'thin', 'hair', 'dotted', 'dashed', 'dashDot', 'dashDotDot',
                 'double', 'medium', 'mediumDashed', 'mediumDashDot',
                 'mediumDashDotDot', 'slantDashDot', 'thick'}


def validate(args: dict, *, max_edits: int = MAX_EDITS) -> dict:
    """The normalised edit, or `SpecError` saying what to fix."""
    sheet = text(args.get('sheet'), 'sheet', 31)
    rows = items(args.get('append_rows'), 'append_rows', MAX_ROWS)
    cells = items(args.get('set_cells'), 'set_cells',
                  max(1, min(max_edits, MAX_EDITS)))
    structural = _validate_structural(args)
    formatting = _validate_format(args.get('format'))
    freeze = _validate_freeze(args.get('freeze'))
    widths = _validate_widths(args.get('widths'))
    if not rows and not cells and not structural and formatting is None \
            and freeze is None and widths is None:
        raise SpecError('Give append_rows, set_cells, insert_rows, delete_rows, '
                        'insert_cols, delete_cols, format, freeze, or widths.')

    clean_rows = []
    for r, row in enumerate(rows, 1):
        if not isinstance(row, list):
            raise SpecError(f'append_rows {r} must be a list of values.')
        clean_rows.append([_value(v, f'append_rows {r}') for v in row])

    clean_cells = []
    for i, cell in enumerate(cells, 1):
        if not isinstance(cell, dict):
            raise SpecError(f'set_cells {i} must be an object with cell and value.')
        ref = str(cell.get('cell') or '').strip().upper()
        if not CELL_REF.match(ref):
            raise SpecError(f'set_cells {i}: {ref!r} is not a cell like B7.')
        clean_cells.append({'cell': ref.replace('$', ''),
                            'value': _value(cell.get('value'), f'set_cells {i}')})
    return {'sheet': sheet, 'append_rows': clean_rows, 'set_cells': clean_cells,
            'structural': structural, 'format': formatting,
            'freeze': freeze, 'widths': widths}


def _count(args: dict, key: str, what: str) -> int:
    raw = args.get(key)
    if raw is None:
        return 0
    try:
        count = int(raw)
    except (TypeError, ValueError):
        raise SpecError(f'{key} must be a whole number of {what} (1 or more).')
    if count < 1:
        raise SpecError(f'{key} must be a whole number of {what} (1 or more).')
    return count


def _validate_structural(args: dict) -> dict:
    """insert/delete rows and columns mid-sheet, each at most once per call."""
    out: dict[str, Any] = {}
    for key, axis in (('insert_rows', 'row'), ('delete_rows', 'row'),
                      ('insert_cols', 'col'), ('delete_cols', 'col')):
        raw = args.get(key)
        if raw is None:
            continue
        if not isinstance(raw, dict):
            raise SpecError(f'{key} must be an object like {{"row": 3, "count": 1}}.')
        if axis == 'row':
            try:
                at = int(raw.get('row'))
            except (TypeError, ValueError):
                raise SpecError(f'{key}.row must be the 1-based row to start at.')
            if at < 1:
                raise SpecError(f'{key}.row must be the 1-based row to start at.')
            out[key] = {'at': at, 'count': _count(raw, 'count', 'rows') or 1}
        else:
            at = _column_index(raw.get('col'), key)
            out[key] = {'at': at, 'count': _count(raw, 'count', 'columns') or 1}
    if sum(v['count'] for k, v in out.items() if k.startswith('insert')) > MAX_ROWS:
        raise SpecError(f'That inserts more than {MAX_ROWS:,} rows and columns together.')
    return out


def _column_index(raw: Any, where: str) -> int:
    """A column letter or 1-based number -> 1-based index."""
    if isinstance(raw, int) and not isinstance(raw, bool) and raw >= 1:
        return raw
    letters = str(raw or '').strip().upper()
    if re.fullmatch(r'[A-Z]{1,3}', letters):
        from openpyxl.utils import column_index_from_string

        return column_index_from_string(letters)
    raise SpecError(f'{where}.col must be a column like C or 3.')


def _validate_format(raw: Any) -> dict | None:
    """A `format` operation: `{range, style}`, or None when absent."""
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise SpecError('format must be an object with range and style.')
    cell_range = str(raw.get('range') or '').strip().upper()
    if not re.fullmatch(r'\$?[A-Z]{1,3}\$?[1-9][0-9]{0,6}(:\$?[A-Z]{1,3}\$?[1-9][0-9]{0,6})?',
                        cell_range):
        raise SpecError(f'format.range {cell_range!r} is not a range like A1:B2.')
    style = raw.get('style')
    if not isinstance(style, dict) or not style:
        raise SpecError('format.style must be a non-empty object.')
    clean: dict[str, Any] = {}
    for flag in ('bold', 'italic', 'wrap'):
        if flag in style:
            if not isinstance(style[flag], bool):
                raise SpecError(f'format.style.{flag} must be true or false.')
            clean[flag] = style[flag]
    for key in ('font_size',):
        if key in style:
            try:
                size = float(style[key])
            except (TypeError, ValueError):
                raise SpecError(f'format.style.{key} must be a number.')
            if not 1 <= size <= 400:
                raise SpecError(f'format.style.{key} must be between 1 and 400.')
            clean[key] = size
    for key in ('font_name', 'font_color', 'fill', 'number_format'):
        if key in style:
            value = text(style[key], f'format.style.{key}', 100)
            if key in ('font_color', 'fill') and not re.fullmatch(r'#?[0-9a-fA-F]{6}', value):
                raise SpecError(f'format.style.{key} must be a color like #2a78d6.')
            clean[key] = value
    if 'alignment' in style:
        alignment = text(style['alignment'], 'format.style.alignment', 20).lower()
        if alignment not in _ALIGNMENTS:
            raise SpecError(f'format.style.alignment must be one of {sorted(_ALIGNMENTS)}.')
        clean['alignment'] = alignment
    if 'border' in style:
        border = style['border']
        if not isinstance(border, dict) or not border:
            raise SpecError('format.style.border must be a non-empty object.')
        clean_border = {}
        for side in ('top', 'right', 'bottom', 'left'):
            if side not in border:
                continue
            named = text(border[side], f'format.style.border.{side}', 20)
            if named not in _BORDER_NAMED:
                raise SpecError(f'format.style.border.{side} must be one of {sorted(_BORDER_NAMED)}.')
            clean_border[side] = named
        if not clean_border:
            raise SpecError('format.style.border names no side (top, right, bottom, left).')
        clean['border'] = clean_border
    if not clean:
        raise SpecError('format.style names nothing this editor styles.')
    return {'range': cell_range.replace('$', ''), 'style': clean}


def _validate_freeze(raw: Any) -> str | None:
    """A `freeze` cell like B2 (or null to unfreeze), or None when absent."""
    if raw is None:
        return None
    if raw is False or raw == '':
        return ''
    cell = str(raw).strip().upper()
    if not CELL_REF.match(cell):
        raise SpecError(f'freeze {cell!r} is not a cell like B2 (rows above and '
                        f'columns left of it stay put). Send null to unfreeze.')
    return cell.replace('$', '')


def _validate_widths(raw: Any) -> dict | None:
    """A `widths` map of column letter to width, or None when absent."""
    if raw is None:
        return None
    if not isinstance(raw, dict) or not raw:
        raise SpecError('widths must be an object like {"A": 20}.')
    clean = {}
    for letter, width in raw.items():
        at = _column_index(letter, 'widths')
        try:
            value = float(width)
        except (TypeError, ValueError):
            raise SpecError(f'widths[{letter!r}] must be a number.')
        if not 0 < value <= 255:
            raise SpecError(f'widths[{letter!r}] must be between 0 and 255.')
        from openpyxl.utils import get_column_letter

        clean[get_column_letter(at)] = value
    return clean


def _value(value: Any, where: str) -> Any:
    if value is not None and not isinstance(value, (str, int, float, bool)):
        raise SpecError(f'{where} has a value that is not text or a number.')
    if isinstance(value, str):
        if len(value) > MAX_CELL_CHARS:
            raise SpecError(f'{where} has a cell of {len(value)} characters; the limit is {MAX_CELL_CHARS}.')
        if value.startswith('=') and _UNSAFE_FORMULA.search(value):
            raise SpecError(
                f'{where} has a formula that reaches outside the workbook. Only '
                f'formulas over the workbook\'s own cells are allowed.'
            )
    return value


def apply(data: bytes, edit: dict) -> tuple[bytes, dict]:
    """Apply `edit` to the workbook in `data`. Returns the new bytes and a report.

    Structural edits run first (rows/columns inserted or deleted, with formula
    references shifted and merges moved), then values, then formatting — so
    `set_cells` coordinates refer to the sheet *after* the structural edits.
    """
    import openpyxl

    try:
        book = openpyxl.load_workbook(io.BytesIO(data))
    except Exception as exc:  # noqa: BLE001 — a corrupt file is an answer, not a crash
        raise SpecError(f'That file could not be opened as a workbook ({exc}).') from exc

    try:
        name = edit['sheet'] or book.sheetnames[0]
        if name not in book.sheetnames:
            raise SpecError(f'No sheet called {name!r}. This workbook has: {", ".join(book.sheetnames)}.')
        sheet = book[name]

        structural = _apply_structural(book, sheet, edit.get('structural') or {})

        # `max_row` counts formatted-but-empty rows on some files, so the first
        # free row is found by looking for the last row that actually has a value.
        last = 0
        for row in sheet.iter_rows():
            if any(cell.value not in (None, '') for cell in row):
                last = row[0].row
        start = last + 1

        for offset, row in enumerate(edit['append_rows']):
            if start + offset > MAX_ROWS + 1:
                raise SpecError(f'That would take {name!r} past {MAX_ROWS:,} rows.')
            if len(row) > MAX_COLUMNS:
                raise SpecError(f'That row has {len(row)} values; the limit is {MAX_COLUMNS}.')
            for column, value in enumerate(row, start=1):
                if value is not None:
                    sheet.cell(row=start + offset, column=column, value=value)

        for cell in edit['set_cells']:
            sheet[cell['cell']] = cell['value']

        if edit.get('format') is not None:
            _apply_format(sheet, edit['format'])
        if edit.get('freeze') is not None:
            sheet.freeze_panes = edit['freeze'] or None
        if edit.get('widths') is not None:
            for letter, width in edit['widths'].items():
                sheet.column_dimensions[letter].width = width

        buf = io.BytesIO()
        book.save(buf)
        return buf.getvalue(), {
            'sheet': name,
            'appended': len(edit['append_rows']),
            'first_new_row': start if edit['append_rows'] else None,
            'cells_set': [c['cell'] for c in edit['set_cells']],
            'structural': structural or None,
            'formatted': edit['format']['range'] if edit.get('format') else None,
            'frozen': edit.get('freeze') or None,
        }
    finally:
        book.close()


def _apply_structural(book, sheet, structural: dict) -> dict:
    """Insert/delete rows and columns, shifting formulas and merges along.

    Merges come off before anything moves: unmerging afterwards would delete
    the moved cells out from under their stale range.
    """
    from .sheets import remerge, unmerge_all

    report: dict[str, Any] = {}
    ops = [(key, axis) for key, axis in (('insert_rows', 'row'), ('delete_rows', 'row'),
                                         ('insert_cols', 'col'), ('delete_cols', 'col'))
           if structural.get(key)]
    if not ops:
        return report
    bounds = unmerge_all(sheet)
    for key, axis in ops:
        op = structural[key]
        at, count = op['at'], op['count']
        delta = count if key.startswith('insert') else -count
        if axis == 'row':
            _check_row_bound(sheet, at, count, key)
            if key == 'insert_rows':
                sheet.insert_rows(at, count)
            else:
                sheet.delete_rows(at, count)
        else:
            _check_col_bound(sheet, at, count, key)
            if key == 'insert_cols':
                sheet.insert_cols(at, count)
            else:
                sheet.delete_cols(at, count)
        _shift_formulas(book, sheet.title, axis, at, delta)
        bounds = _shift_bounds(bounds, axis, at, delta)
        report[key] = op
    remerge(sheet, bounds)
    return report


def _check_row_bound(sheet, at: int, count: int, key: str) -> None:
    if at > (sheet.max_row or 0) + 1 + count:
        raise SpecError(f'{key}.row {at} starts past the end of the sheet.')
    if (sheet.max_row or 0) + count > MAX_ROWS + 1:
        raise SpecError(f'That would take the sheet past {MAX_ROWS:,} rows.')


def _check_col_bound(sheet, at: int, count: int, key: str) -> None:
    if (sheet.max_column or 0) + count > MAX_COLUMNS + 1:
        raise SpecError(f'That would take the sheet past {MAX_COLUMNS} columns.')


def _shift_formulas(book, edited: str, axis: str, at: int, delta: int) -> None:
    """Move formula references past a structural edit, in every sheet.

    openpyxl moves the cells but leaves the text of formulas alone, so without
    this a row inserted above a total silently repoints it. Absolute (`$`)
    references shift like relative ones when they sit past the edit — they say
    "this row", and the row moved. (openpyxl's own `Translator` is copy
    semantics and deliberately leaves `$` references alone, so it is the wrong
    tool here.) A reference into a deleted band becomes `#REF!`, which the
    evaluator refuses loudly rather than guessing.
    """
    count = abs(delta)
    for ws in book.worksheets:
        for row in ws.iter_rows():
            for cell in row:
                value = cell.value
                if not (isinstance(value, str) and value.startswith('=')):
                    continue
                shifted = _shift_text(value, edited, ws.title, axis, at, delta, count)
                if shifted != value:
                    cell.value = shifted


_REF_SHIFT_RE = re.compile(
    # Left-anchored, so `LOG10(` is not read as a reference to `OG10`.
    r"(?<![A-Za-z0-9_.$])(?P<sheet>'[^']+'|[A-Za-z_][\w.]*!)?"
    r"(?P<coldollar>\$?)(?P<col>[A-Z]{1,3})"
    r"(?P<rowdollar>\$?)(?P<row>[1-9][0-9]{0,6})")


def _shift_text(formula: str, edited: str, sheet: str,
                axis: str, at: int, delta: int, count: int) -> str:
    """Shift the references of one formula past the edit, span by span."""
    from openpyxl.utils import column_index_from_string, get_column_letter

    def shift(match: re.Match) -> str:
        prefix = match.group('sheet') or ''
        owner = prefix[:-1].strip("'") if prefix else None
        if owner is not None and owner != edited:
            return match.group(0)
        if owner is None and sheet != edited:
            return match.group(0)
        col = column_index_from_string(match.group('col'))
        row = int(match.group('row'))
        if axis == 'row':
            row = _shift_index(row, at, delta, count)
            if row is None:
                return f'{prefix}#REF!'
        else:
            moved = _shift_index(col, at, delta, count)
            if moved is None:
                return f'{prefix}#REF!'
            col = moved
        return (f'{prefix}{match.group("coldollar")}{get_column_letter(col)}'
                f'{match.group("rowdollar")}{row}')

    # Quoted spans are strings, not references (`"A1"` stays `"A1"`); this
    # mirrors `inference/formulas._split_code`.
    out: list[str] = []
    i, n = 0, len(formula)
    while i < n:
        if formula[i] == '"':
            j = formula.find('"', i + 1)
            j = n if j == -1 else j + 1
            out.append(formula[i:j])
            i = j
        else:
            j = formula.find('"', i)
            span = formula[i:] if j == -1 else formula[i:j]
            out.append(_REF_SHIFT_RE.sub(shift, span))
            i = n if j == -1 else j
    return ''.join(out)


def _shift_index(index: int, at: int, delta: int, count: int) -> int | None:
    """The new 1-based index past the edit, or None when it fell inside a
    deleted band."""
    if delta > 0:
        return index + delta if index >= at else index
    if index >= at + count:
        return index + delta
    if index >= at:
        return None
    return index


def _shift_bounds(bounds: list[tuple[int, int, int, int]], axis: str,
                  at: int, delta: int) -> list[tuple[int, int, int, int]]:
    """Move merge bounds past a structural edit.

    Ranges straddling a delete shrink to what is left; ones a delete
    collapses are dropped by the re-merge.
    """
    out = []
    for min_col, min_row, max_col, max_row in bounds:
        if axis == 'row':
            if min_row >= at:
                min_row = max(at, min_row + delta)
            if max_row >= at:
                max_row = max(at - 1, max_row + delta)
        else:
            if min_col >= at:
                min_col = max(at, min_col + delta)
            if max_col >= at:
                max_col = max(at - 1, max_col + delta)
        out.append((min_col, min_row, max_col, max_row))
    return out


def _apply_format(sheet, formatting: dict) -> None:
    """Paint one range: bold/italic/fonts/colors/fill/alignment/numbers/borders."""
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.styles import Color as OoxmlColor

    style = formatting['style']
    font_kwargs: dict[str, Any] = {}
    if 'bold' in style:
        font_kwargs['bold'] = style['bold']
    if 'italic' in style:
        font_kwargs['italic'] = style['italic']
    if 'font_size' in style:
        font_kwargs['size'] = style['font_size']
    if 'font_name' in style:
        font_kwargs['name'] = style['font_name']
    if 'font_color' in style:
        font_kwargs['color'] = OoxmlColor(rgb='FF' + style['font_color'].lstrip('#').upper())
    font = Font(**font_kwargs) if font_kwargs else None
    fill = PatternFill(patternType='solid',
                       fgColor=OoxmlColor(rgb='FF' + style['fill'].lstrip('#').upper())) \
        if 'fill' in style else None
    alignment = None
    if 'alignment' in style or 'wrap' in style:
        horizontal = style.get('alignment') if style.get('alignment') in (
            'left', 'center', 'right', 'justify', 'distributed') else None
        vertical = style.get('alignment') if style.get('alignment') in (
            'top', 'bottom', 'middle') else None
        alignment = Alignment(horizontal=horizontal, vertical=vertical,
                              wrap_text=style.get('wrap'))
    border = None
    if 'border' in style:
        sides = {side: Side(style=named) for side, named in style['border'].items()}
        border = Border(**{k: sides.get(k) for k in ('left', 'right', 'top', 'bottom')})
    number_format = style.get('number_format')

    for row in sheet[formatting['range']]:
        cells = row if isinstance(row, tuple) else (row,)
        for cell in cells:
            if font is not None:
                cell.font = font
            if fill is not None:
                cell.fill = fill
            if alignment is not None:
                cell.alignment = alignment
            if border is not None:
                cell.border = border
            if number_format is not None:
                cell.number_format = number_format
