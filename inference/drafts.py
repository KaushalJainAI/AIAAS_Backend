"""
Office drafts: autosave without rebuilding the file on every keystroke.

Rebuilding a `.docx` / `.pptx` / `.xlsx` costs a full render through
python-docx / python-pptx / openpyxl — too heavy for the 913 MB box at
one render per keystroke pause. So an app autosave stores its editor state
cheaply (`metadata.draft`: the spec or the grid) and the real bytes are
rebuilt later:

* by a debounced background task after `DRAFT_RENDER_QUIET_SECONDS` of quiet
  (scheduled by the draft view through `background.spawn()`), and
* on any read that needs the bytes: download, export, copy, the office grid,
  or an agent read. Those call `ensure_rendered(doc)` first.

Three rules carry it.

* **A draft is an `app` version like any save.** The render goes through the
  ordinary edit paths (`office_edit.edit_spec` / `edit_workbook`), which
  snapshot with the usual coalescing — a burst of autosaves still makes one
  version, and an agent write after them still gets its own.
* **A render never clobbers a newer writer.** Any real overwrite
  (`office_edit.replace_bytes`, and the text branch of `save_text`) clears a
  pending draft, so an agent's write between draft and render wins and the
  stale draft dies instead of overwriting it. And a render only runs when the
  draft it was scheduled for is still the latest one.
* **The app's etag survives the background render.** The render bumps
  `updated_at`, which the app has not seen — so the next save would 412
  against an `updated_at` that moved under it. The render therefore leaves
  `metadata.last_render = {base, updated}` in the same transaction, and
  `office_edit.is_stale` (every guarded write, not just drafts) accepts an
  etag equal to `base` while `updated_at` still equals `updated`. Any other
  write after the render moves `updated_at` on and closes that door, so a
  second tab (or an agent) that saved in between still gets its 412.
* **A render never wipes a newer draft.** It writes only if the draft it
  rendered is still the stored one, checked under the row lock
  (`replace_bytes(render_of=...)`); a draft saved mid-render supersedes it,
  and its own task renders it later. Draft saves are a locked
  read-modify-write for the same reason — they touch only the draft keys,
  never a `spec` or `last_render` a render just wrote.
"""
from __future__ import annotations

import logging
from datetime import datetime

from django.db import transaction
from django.utils import timezone

from .models import Document

logger = logging.getLogger(__name__)

#: Payload kinds `save_draft` accepts, per file type.
SPEC_TYPES = ('docx', 'pptx')
GRID_TYPES = ('xlsx',)


class DraftError(Exception):
    """A refusal the caller can act on. `status` is the HTTP answer."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def _limits():
    from workflow_backend import thresholds as t

    return t.DRAFT_RENDER_QUIET_SECONDS


def _parse(when: str | None) -> datetime | None:
    if not when:
        return None
    from django.utils.dateparse import parse_datetime

    try:
        return parse_datetime(str(when).strip())
    except (ValueError, TypeError):
        return None


def save_draft(doc: Document, payload: dict, expected: str | None) -> tuple[dict, str]:
    """Store the editor's state cheaply. Returns (serialised doc, draft stamp).

    `payload` holds `spec` (a deck or Word file) or `grid` (a workbook's
    sheets). Full validation happens at render; here the shape is checked just
    enough to refuse a draft that could never render. Takes the same
    `expected_updated_at` / `If-Match` guard as every other write.
    """
    from . import office_edit

    if not isinstance(payload, dict):
        raise DraftError('Send the edited file as an object with `spec`, `grid` or `snapshot`.')
    if doc.file_type in SPEC_TYPES:
        kind, body = 'spec', payload.get('spec')
        if not isinstance(body, dict):
            raise DraftError('Send the edited file as `spec`.')
    elif doc.file_type in GRID_TYPES:
        if isinstance(payload.get('snapshot'), dict):
            # The Sheets app posts what Univer returns, whole.
            kind, body = 'snapshot', payload.get('snapshot')
        else:
            kind, body = 'grid', payload.get('grid')
            if not isinstance(body, dict) or not isinstance(body.get('sheets'), list):
                raise DraftError('Send the edited workbook as `grid` with `sheets`, '
                                 'or as `snapshot`.')
    else:
        raise DraftError(
            f'Drafts are for office files; this is a {doc.file_type} file, saved directly.',
            400,
        )

    with transaction.atomic():
        # Re-read under the lock: a render may have finished since the view
        # loaded `doc`, and both the guard and the metadata must see it.
        fresh = (Document.objects.select_for_update()
                 .filter(id=doc.id).values('metadata', 'updated_at').first())
        if fresh is None:
            raise DraftError('This file no longer exists.', 404)
        doc.metadata, doc.updated_at = fresh['metadata'], fresh['updated_at']
        if office_edit.is_stale(doc, expected):
            raise DraftError(
                'This file changed since you opened it. Reload it, then keep editing.',
                412,
            )

        now = timezone.now()
        metadata = dict(doc.metadata or {})
        metadata['draft'] = {
            'kind': kind,
            'payload': body,
            'saved_at': now.isoformat(),
            'base': doc.updated_at.isoformat(),
        }
        metadata.pop('draft_error', None)
        doc.metadata = metadata
        # A normal save: `updated_at` moves, so listings stay fresh and the
        # next guard has something to check against.
        doc.save(update_fields=['metadata', 'updated_at'])
    return doc, now.isoformat()


def maybe_render_draft(doc_id: int, seen: str | None = None, *, force: bool = False) -> bool:
    """Render the pending draft when it is due. Returns whether it rendered.

    `seen` is the stamp the background task was scheduled with: a newer draft
    means this task is superseded and bows out, so one burst renders once.
    `force` skips the quiet check for readers that need the bytes now.
    Read through `timezone.now()` so tests can move the clock.
    """
    try:
        doc = Document.objects.get(id=doc_id)
    except Document.DoesNotExist:
        return False
    draft = (doc.metadata or {}).get('draft')
    if not isinstance(draft, dict):
        return False
    if seen is not None and draft.get('saved_at') != seen:
        return False
    if not force:
        saved_at = _parse(draft.get('saved_at'))
        if saved_at is None or (timezone.now() - saved_at).total_seconds() < _limits():
            return False
    from .office_edit import DraftSuperseded

    try:
        _render(doc, draft)
    except DraftSuperseded:
        # A newer draft (or a real write) landed while this one rendered;
        # the newer draft's own task renders it.
        return False
    except Exception:  # noqa: BLE001 — a failed render keeps the draft, never breaks the file
        logger.exception('Could not render the draft of document %s', doc_id)
        try:
            with transaction.atomic():
                row = Document.objects.select_for_update().filter(id=doc_id).first()
                metadata = dict((row.metadata if row else None) or {})
                # Only mark the draft that failed, never a newer one.
                if (metadata.get('draft') or {}).get('saved_at') == draft.get('saved_at'):
                    metadata['draft_error'] = 'The last autosave could not be rendered yet.'
                    Document.objects.filter(id=doc_id).update(metadata=metadata)
        except Exception:  # noqa: BLE001 — see above
            logger.exception('Could not record the draft error of document %s', doc_id)
        return False
    return True


def ensure_rendered(doc: Document) -> Document:
    """The pending draft, rendered now — for readers that need the bytes.

    A no-op (same object, no query) when nothing is pending.
    """
    if not isinstance((doc.metadata or {}).get('draft'), dict):
        return doc
    if maybe_render_draft(doc.id, force=True):
        doc.refresh_from_db()
    else:
        # A failed render leaves the draft; serve what the bytes still hold
        # rather than refusing the read for an autosave problem.
        doc.refresh_from_db()
    return doc


def _render(doc: Document, draft: dict) -> None:
    """Apply one draft through the ordinary edit paths (which snapshot it)."""
    from . import office_edit

    # `replace_bytes(render_of=...)` writes only if this draft is still the
    # stored one, and leaves the `last_render` door in the same transaction.
    stamp = draft.get('saved_at')
    kind = draft.get('kind')
    payload = draft.get('payload')
    if kind == 'spec':
        office_edit.edit_spec(doc, payload, render_of=stamp)
    elif kind == 'grid':
        _apply_grid(doc, payload, stamp)
    elif kind == 'snapshot':
        _apply_snapshot(doc, payload, stamp)
    else:  # pragma: no cover — save_draft only writes the kinds above
        raise DraftError(f'Unknown draft kind {kind!r}.')
    doc.refresh_from_db()


def _apply_grid(doc: Document, grid: dict, stamp: str | None = None) -> None:
    """A workbook draft's sheets onto the stored file, sheet by sheet."""
    from chat.tools.office import edit as office_edit_tool
    from chat.tools.office.spec import SpecError

    from . import office_edit

    data = office_edit._read_bytes(doc)
    for sheet in grid.get('sheets') or []:
        name = sheet.get('name')
        rows = sheet.get('rows')
        if not isinstance(name, str) or not isinstance(rows, list):
            raise DraftError('Each drafted sheet needs a `name` and `rows`.')
        current = _sheet_cells(data, name)
        edits = _diff_cells(current, rows)
        if not edits:
            continue
        try:
            change = office_edit_tool.validate({'sheet': name, 'set_cells': edits})
            data, _report = office_edit_tool.apply(data, change)
        except SpecError as exc:
            raise DraftError(str(exc)) from exc
    from .utils import extract_xlsx_text
    import io

    text = extract_xlsx_text(io.BytesIO(data))
    office_edit.replace_bytes(doc, data, text=text, render_of=stamp)


def _apply_snapshot(doc: Document, snapshot: dict, stamp: str | None = None) -> None:
    """A Univer snapshot onto the stored workbook, through `sheets.py`."""
    from . import office_edit, sheets
    from .utils import extract_xlsx_text

    try:
        data = sheets.apply_snapshot(office_edit._read_bytes(doc), snapshot)
    except sheets.SnapshotError as exc:
        raise DraftError(str(exc)) from exc
    import io

    text = extract_xlsx_text(io.BytesIO(data))
    office_edit.replace_bytes(doc, data, text=text, render_of=stamp)


def _sheet_cells(data: bytes, name: str) -> list[list]:
    import io

    import openpyxl

    book = openpyxl.load_workbook(io.BytesIO(data))
    try:
        if name not in book.sheetnames:
            return []
        ws = book[name]
        return [[c.value for c in row] for row in ws.iter_rows()]
    finally:
        book.close()


def _diff_cells(current: list[list], wanted: list) -> list[dict]:
    """Cell edits turning `current` into the drafted grid."""
    edits = []
    for r, row in enumerate(wanted):
        if not isinstance(row, list):
            continue
        for c, value in enumerate(row):
            old = current[r][c] if r < len(current) and c < len(current[r]) else None
            if _cell_text(old) != _cell_text(value):
                edits.append({'cell': f'{_col(c)}{r + 1}', 'value': value})
    return edits


def _col(index: int) -> str:
    name = ''
    index += 1
    while index:
        index, rest = divmod(index - 1, 26)
        name = chr(65 + rest) + name
    return name


def _cell_text(value) -> str:
    if value is None:
        return ''
    if isinstance(value, bool):
        return str(value)
    return str(value)
