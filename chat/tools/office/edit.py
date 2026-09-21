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
from .workbook import MAX_CELL_CHARS, MAX_ROWS, _UNSAFE_FORMULA

CELL_REF = re.compile(r'^\$?[A-Z]{1,3}\$?[1-9][0-9]{0,6}$')
#: Workspace knob (`edit_workbook.maxEdits`); the constant stays as the floor
#: under a failed overlay read — validation clamps the knob to never exceed it.
MAX_EDITS = 200


def validate(args: dict, *, max_edits: int = MAX_EDITS) -> dict:
    """The normalised edit, or `SpecError` saying what to fix."""
    sheet = text(args.get('sheet'), 'sheet', 31)
    rows = items(args.get('append_rows'), 'append_rows', MAX_ROWS)
    cells = items(args.get('set_cells'), 'set_cells',
                  max(1, min(max_edits, MAX_EDITS)))
    if not rows and not cells:
        raise SpecError('Give append_rows, set_cells, or both.')

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
    return {'sheet': sheet, 'append_rows': clean_rows, 'set_cells': clean_cells}


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
    """Apply `edit` to the workbook in `data`. Returns the new bytes and a report."""
    import openpyxl

    try:
        book = openpyxl.load_workbook(io.BytesIO(data))
    except Exception as exc:  # noqa: BLE001 — a corrupt file is an answer, not a crash
        raise SpecError(f'That file could not be opened as a workbook ({exc}).') from exc

    name = edit['sheet'] or book.sheetnames[0]
    if name not in book.sheetnames:
        raise SpecError(f'No sheet called {name!r}. This workbook has: {", ".join(book.sheetnames)}.')
    sheet = book[name]

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
        for column, value in enumerate(row, start=1):
            if value is not None:
                sheet.cell(row=start + offset, column=column, value=value)

    for cell in edit['set_cells']:
        sheet[cell['cell']] = cell['value']

    buf = io.BytesIO()
    book.save(buf)
    book.close()
    return buf.getvalue(), {
        'sheet': name,
        'appended': len(edit['append_rows']),
        'first_new_row': start if edit['append_rows'] else None,
        'cells_set': [c['cell'] for c in edit['set_cells']],
    }
