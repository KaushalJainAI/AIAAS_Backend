"""
What the in-browser apps do to a file: create it, rename it, and make basic
edits to the office formats.

The text formats already had a door (`document_content`); this is the rest of
the productivity suite. Three rules carry it, each borrowed from the office
tools rather than invented here:

* **An office file is edited through the code that renders it.** A workbook is
  changed cell by cell with `office/edit.py::apply` — the same openpyxl path
  `edit_workbook` uses, so formatting, charts and untouched formulas survive.
  A deck or a Word file is edited as its stored *spec* and re-rendered by
  `deck.render` / `document.render`, because python-pptx cannot faithfully
  round-trip arbitrary edits to a file it did not lay out. An uploaded
  `.docx`/`.pptx` has no spec until `inference/importers.py` converts it
  (best effort, original kept as version 1) — editing its extracted text
  would save something that is not the file.
* **Bytes are replaced in place and the old bytes removed after the row
  commits** — the other order leaves a row pointing at nothing, which
  downloads as a 500 (the rule `vfs.write_binary` follows).
* **The caller's timestamp guards every write**, compared as an instant:
  DRF serialises UTC as `...Z` and Python's `isoformat()` writes `+00:00`, so
  comparing strings refused every save the browser ever made.

Creating a file is not an upload: nothing arrives from the user's disk, so
nothing is indexed — a new blank file is `stored`, like one an agent writes.
"""
from __future__ import annotations

import io
from typing import Any

from django.core.files.base import ContentFile
from django.db import transaction
from django.utils.dateparse import parse_datetime

from office import formulas
from .models import Document
from .utils import TEXT_FILE_TYPES

#: What a new file may be, by extension → `Document.file_type`.
NEW_TEXT_TYPES = {
    'txt': 'txt', 'md': 'md', 'csv': 'csv', 'tsv': 'csv', 'json': 'json',
    'html': 'html', 'htm': 'html', 'py': 'txt', 'js': 'txt', 'ts': 'txt',
    'css': 'txt', 'sql': 'txt', 'yaml': 'txt', 'yml': 'txt', 'xml': 'txt',
    'sh': 'txt', 'svg': 'txt',
}
NEW_OFFICE_TYPES = {'docx', 'pptx', 'xlsx'}

#: The grid the Sheets app is sent. A workbook bigger than this is still
#: editable cell by cell; the response says it was cut.
GRID_MAX_ROWS = 500
GRID_MAX_COLS = 40

_FREE_NAME_TRIES = 50


class EditError(Exception):
    """A refusal the caller can act on. `status` is the HTTP answer."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def extension(name: str) -> str:
    _, dot, ext = (name or '').rpartition('.')
    return ext.lower() if dot else ''


def is_stale(doc: Document, expected: str | None) -> bool:
    """True when the caller opened an older version than the one stored.

    One exception, shared by every guarded write: the background render of
    the caller's own draft (`inference/drafts.py`) moves `updated_at` under
    an app that has not seen it. That render leaves `metadata.last_render =
    {base, updated}`, and an etag equal to `base` is still fresh while
    `updated_at` still equals `updated` — any other write since moves
    `updated_at` on and closes the door. Checked here rather than in the
    draft view alone, or a Ctrl+S, restore or import after a render is a 412
    nobody caused.
    """
    if not expected:
        return False
    when = parse_datetime(str(expected).strip())
    if when is None:
        # Unparseable is treated as stale: a guard that waves through what it
        # cannot read is not a guard.
        return True
    if when == doc.updated_at:
        return False
    last = (doc.metadata or {}).get('last_render')
    if not isinstance(last, dict):
        return True
    base = parse_datetime(str(last.get('base') or ''))
    updated = parse_datetime(str(last.get('updated') or ''))
    return not (base is not None and updated is not None
                and when == base and doc.updated_at == updated)


def _taken(user, folder, name: str, exclude_id: int | None = None) -> bool:
    qs = Document.objects.filter(user=user, folder=folder, name=name)
    if exclude_id is not None:
        qs = qs.exclude(id=exclude_id)
    return qs.exists()


def free_name(user, folder, name: str) -> str:
    """`name`, or the first `stem (n).ext` not taken in `folder`."""
    if not _taken(user, folder, name):
        return name
    stem, dot, ext = name.rpartition('.')
    if not dot:
        stem, ext = name, ''
    for n in range(2, _FREE_NAME_TRIES + 2):
        candidate = f'{stem} ({n}).{ext}' if ext else f'{stem} ({n})'
        if not _taken(user, folder, candidate):
            return candidate
    raise EditError(f'{name} and {_FREE_NAME_TRIES} numbered copies of it already exist.', 409)


def _clean_name(raw: Any) -> str:
    from .vfs import safe_name

    name = safe_name(str(raw or ''))
    if not name:
        raise EditError('Give the file a name.')
    return name


# ---------------------------------------------------------------------------
# Rename
# ---------------------------------------------------------------------------

def rename(doc: Document, raw_name: Any) -> Document:
    """Rename in place. The extension is kept, because it decides the type."""
    name = _clean_name(raw_name)
    if extension(name) != extension(doc.name):
        old = extension(doc.name)
        raise EditError(
            f'Keep the .{old} extension — it decides how the file opens.' if old
            else 'This file has no extension; rename it without adding one.'
        )
    if name == doc.name:
        return doc
    if _taken(doc.user, doc.folder, name, exclude_id=doc.id):
        raise EditError(f'There is already a file called {name} here.', 409)
    # A rename is not a content change, so `updated_at` — the etag every open
    # editor guards its saves with — stays put. Moving it made the next
    # autosave of the file being renamed a 412 nobody caused. A queryset
    # update, because `save()` stamps `auto_now` on the instance even when the
    # column is left out of `update_fields`.
    Document.objects.filter(id=doc.id).update(name=name)
    doc.name = name
    return doc


# ---------------------------------------------------------------------------
# Replacing bytes
# ---------------------------------------------------------------------------

class DraftSuperseded(Exception):
    """A draft render found a newer draft (or a real write) under it."""


def replace_bytes(doc: Document, data: bytes, *, text: str | None = None,
                  spec: dict | None = None, version_source: str | None = 'app',
                  render_of: str | None = None) -> Document:
    """Swap the stored file for `data`; drop the old bytes once the row commits.

    What the file held is kept as a version first (`inference/versions.py`);
    `version_source=None` is for a caller that already took one (a restore).

    A real overwrite also clears a pending office draft (`inference/drafts.py`):
    the bytes just replaced whatever the draft was building on, so rendering
    it afterwards would clobber this write.

    `render_of` is set by the draft render alone: the `saved_at` of the draft
    it rendered. Under the row lock, the write goes ahead only if that draft
    is still the stored one — a draft saved while the render ran is newer
    than these bytes, and wiping it would lose the edit and 412 the app's
    next save. The render also leaves `last_render` in the same transaction
    (see `is_stale`), so there is no moment where the app's etag is refused.
    """
    from workflow_backend.thresholds import DOCUMENT_EXTRACT_CAP

    if version_source:
        from . import versions

        versions.snapshot(doc, version_source)
    old_name = doc.file.name if doc.file else ''
    storage = doc.file.storage
    doc.file.save(doc.name, ContentFile(bytes(data)), save=False)
    doc.file_size = len(data)
    fields = ['file', 'file_size', 'updated_at']
    if text is not None:
        doc.content_text = text[:DOCUMENT_EXTRACT_CAP]
        fields.append('content_text')
    try:
        with transaction.atomic():
            base = None
            if render_of is not None:
                current = (Document.objects.select_for_update()
                           .filter(id=doc.id).values('metadata', 'updated_at').first())
                draft = ((current or {}).get('metadata') or {}).get('draft')
                if not isinstance(draft, dict) or draft.get('saved_at') != render_of:
                    raise DraftSuperseded()
                doc.metadata = current['metadata']
                base = current['updated_at']
            metadata = dict(doc.metadata or {})
            for key in ('draft', 'draft_error', 'last_render'):
                metadata.pop(key, None)
            if spec is not None:
                metadata['spec'] = spec
            if metadata != (doc.metadata or {}):
                doc.metadata = metadata
                fields.append('metadata')
            doc.save(update_fields=fields)
            if base is not None:
                doc.metadata = {**metadata, 'last_render': {
                    'base': base.isoformat(), 'updated': doc.updated_at.isoformat()}}
                # `update` skips `auto_now`: `updated_at` stays the render's.
                Document.objects.filter(id=doc.id).update(metadata=doc.metadata)
    except Exception:
        doc.file.delete(save=False)
        raise
    if old_name and old_name != doc.file.name:
        try:
            storage.delete(old_name)
        except Exception:  # noqa: BLE001 — an orphaned blob is not worth failing a save
            pass
    return doc


def save_text(doc: Document, content: str, *, version_source: str = 'app') -> Document:
    """A text file's new contents, written to *both* places a reader looks.

    An uploaded `.md` keeps its bytes in `file`, and `document_download` serves
    those bytes in preference to `content_text` — so writing only the text
    column saved an edit that no preview or download ever showed.
    """
    if doc.file:
        return replace_bytes(doc, content.encode('utf-8'), text=content,
                             version_source=version_source)
    from . import versions

    versions.snapshot(doc, version_source)
    doc.content_text = content
    doc.file_size = len(content.encode('utf-8'))
    fields = ['content_text', 'file_size', 'updated_at']
    metadata = dict(doc.metadata or {})
    for key in ('draft', 'draft_error', 'last_render'):
        metadata.pop(key, None)
    if metadata != (doc.metadata or {}):
        doc.metadata = metadata
        fields.append('metadata')
    doc.save(update_fields=fields)
    return doc


# ---------------------------------------------------------------------------
# Workbooks
# ---------------------------------------------------------------------------

def _read_bytes(doc: Document) -> bytes:
    if not doc.file:
        raise EditError('This file has no stored bytes to edit.')
    with doc.file.open('rb') as fh:
        return fh.read()


def _cell_out(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    # Dates, times, decimals: shown as text; writing them back stores text.
    return str(value)


def workbook_grid(doc: Document) -> dict:
    """Every sheet as a grid of raw values — formulas as their `=` source.

    `values` rides beside `rows` with each formula calculated
    (`office/formulas.py`; unevaluable stays None): the preview and the
    app show numbers, not the text of the formula that makes them.
    """
    import openpyxl

    if doc.file_type != 'xlsx':
        raise EditError('Only .xlsx workbooks have a grid.')
    try:
        book = openpyxl.load_workbook(io.BytesIO(_read_bytes(doc)))
    except EditError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise EditError(f'This file could not be opened as a workbook ({exc}).') from exc

    try:
        sheets = []
        for ws in book.worksheets:
            max_row, max_col = ws.max_row or 0, ws.max_column or 0
            rows = []
            values = []
            for row in ws.iter_rows(min_row=1, max_row=min(max_row, GRID_MAX_ROWS),
                                    max_col=min(max_col, GRID_MAX_COLS)):
                raw = [_cell_out(c.value) for c in row]
                rows.append(raw)
                values.append([_calculated(book, ws.title, c) for c in row])
            # Trailing empty rows are formatting, not data.
            while rows and all(v in (None, '') for v in rows[-1]):
                rows.pop()
                values.pop()
            sheets.append({
                'name': ws.title,
                'rows': rows,
                'values': values,
                'row_count': max_row,
                'col_count': max_col,
                'truncated': max_row > GRID_MAX_ROWS or max_col > GRID_MAX_COLS,
            })
        return {'sheets': sheets, 'updated_at': doc.updated_at}
    finally:
        book.close()


def _calculated(book, sheet: str, cell) -> Any:
    """A cell for the `values` grid: formulas computed, the rest as stored."""
    raw = cell.value
    if isinstance(raw, str) and raw.startswith('='):
        try:
            return formulas.json_value(formulas.cell(book, sheet, cell.coordinate))
        except formulas.FormulaError:
            return None
    return formulas.json_value(_cell_out(raw))


def edit_workbook(doc: Document, payload: dict) -> Document:
    from office import edit
    from office.spec import SpecError

    if doc.file_type != 'xlsx':
        raise EditError('Cell edits apply to .xlsx workbooks.')
    try:
        change = edit.validate(payload)
        data, _report = edit.apply(_read_bytes(doc), change)
    except SpecError as exc:
        raise EditError(str(exc)) from exc
    from .utils import extract_xlsx_text

    text = extract_xlsx_text(io.BytesIO(data))
    doc = replace_bytes(doc, data, text=text)
    # The stored spec described the file as rendered; after a cell edit it
    # describes a file that no longer exists, and the preview would draw it.
    if (doc.metadata or {}).get('spec'):
        doc.metadata = {k: v for k, v in doc.metadata.items() if k != 'spec'}
        doc.save(update_fields=['metadata', 'updated_at'])
    return doc


# ---------------------------------------------------------------------------
# Decks and Word files, through their spec
# ---------------------------------------------------------------------------

def _images_for(doc: Document, paths: list[str]) -> dict[str, bytes]:
    """Load the images a spec embeds, from the owner's whole tree."""
    if not paths:
        return {}
    from .vfs import VfsError, build_scope, read_image

    scope = build_scope(doc.user, 'readonly')
    try:
        return {p: read_image(scope, p)[0] for p in dict.fromkeys(paths)}
    except VfsError as exc:
        raise EditError(
            f'This file embeds an image that can no longer be found ({exc}). '
            f'Remove that slide or block to save.'
        ) from exc


def edit_spec(doc: Document, raw: Any, *, version_source: str = 'app',
              render_of: str | None = None) -> Document:
    from office import deck, document
    from office.spec import SpecError

    stored = (doc.metadata or {}).get('spec')
    if doc.file_type not in ('pptx', 'docx') or not isinstance(stored, dict):
        raise EditError(
            'Only decks and Word files made in this workspace can be edited here. '
            'An uploaded one keeps its own layout, which this editor cannot rebuild — '
            'download it to edit it.'
        )
    if not isinstance(raw, dict):
        raise EditError('Send the edited file as `spec`.')

    try:
        if doc.file_type == 'pptx':
            theme = raw.get('theme') or stored.get('theme')
            if isinstance(theme, dict):
                theme = theme.get('name')
            spec = deck.validate({'title': raw.get('title', stored.get('title')),
                                  'theme': theme, 'slides': raw.get('slides')})
            data = deck.render(spec, _images_for(doc, deck.image_paths(spec)))
            return replace_bytes(doc, data, text=deck.extract_text(spec), spec=deck.preview(spec),
                                 version_source=version_source, render_of=render_of)

        spec = document.validate({
            'title': raw.get('title', stored.get('title')),
            'subtitle': raw.get('subtitle', stored.get('subtitle')),
            'theme': raw.get('theme') or stored.get('theme'),
            'blocks': raw.get('blocks'),
        })
        data = document.render(spec, _images_for(doc, document.image_paths(spec)))
        return replace_bytes(doc, data, text=document.extract_text(spec),
                             spec=document.preview(spec), version_source=version_source,
                             render_of=render_of)
    except SpecError as exc:
        raise EditError(str(exc)) from exc


# ---------------------------------------------------------------------------
# New files
# ---------------------------------------------------------------------------

def _blank_office(kind: str, title: str) -> tuple[bytes, str, dict]:
    from office import deck, document, workbook

    if kind == 'pptx':
        spec = deck.validate({'title': title, 'slides': [
            {'layout': 'title', 'title': title, 'subtitle': ''},
        ]})
        return deck.render(spec, {}), deck.extract_text(spec), deck.preview(spec)
    if kind == 'docx':
        spec = document.validate({'title': title, 'blocks': [
            {'type': 'paragraph', 'text': 'Start writing here.'},
        ]})
        return document.render(spec, {}), document.extract_text(spec), document.preview(spec)
    spec = workbook.validate({'sheets': [
        {'name': 'Sheet1', 'columns': ['Column A', 'Column B', 'Column C'], 'rows': [['', '', '']]},
    ]})
    data, _ = workbook.render(spec)
    # No spec kept: the Sheets app edits a workbook from its cells, and a
    # spec would describe the file only until the first edit.
    return data, workbook.extract_text(spec), {}


def create(user, raw_name: Any, folder, content: str = '') -> Document:
    name = _clean_name(raw_name)
    ext = extension(name)
    if ext not in NEW_TEXT_TYPES and ext not in NEW_OFFICE_TYPES:
        raise EditError(
            f'A new file needs one of these extensions: '
            f'{", ".join(sorted(set(NEW_TEXT_TYPES) | NEW_OFFICE_TYPES))}.'
        )
    if not isinstance(content, str):
        raise EditError('`content` must be text.')
    name = free_name(user, folder, name)

    if ext in NEW_TEXT_TYPES:
        return Document.objects.create(
            user=user, folder=folder, name=name, file_type=NEW_TEXT_TYPES[ext],
            content_text=content, file_size=len(content.encode('utf-8')),
            status='stored', metadata={'created_by': 'user'},
        )

    data, text, spec = _blank_office(ext, name.rpartition('.')[0] or name)
    doc = Document(
        user=user, folder=folder, name=name, file_type=ext, file_size=len(data),
        content_text=text, status='stored',
        metadata={'created_by': 'user', **({'spec': spec} if spec else {})},
    )
    doc.file.save(name, ContentFile(data), save=False)
    try:
        with transaction.atomic():
            doc.save()
    except Exception:
        doc.file.delete(save=False)
        raise
    return doc


def is_text(doc: Document) -> bool:
    return doc.file_type in TEXT_FILE_TYPES


# ---------------------------------------------------------------------------
# Copy
# ---------------------------------------------------------------------------

def copy(doc: Document, folder) -> Document:
    """A second file with the same bytes, text and spec, in `folder`.

    Pasted into its own folder it becomes `name - Copy.ext`, as a desktop file
    browser names it; anywhere else it keeps its name unless that is taken.
    The copy is `stored` and never indexed: duplicating a file must not start a
    second embedding job for text the index already holds.
    """
    name = doc.name
    if folder == doc.folder:
        stem, dot, ext = name.rpartition('.')
        name = f'{stem} - Copy.{ext}' if dot else f'{name} - Copy'
    name = free_name(doc.user, folder, name)

    clone = Document(
        user=doc.user, folder=folder, name=name, file_type=doc.file_type,
        file_size=doc.file_size, content_text=doc.content_text, status='stored',
        metadata={**(doc.metadata or {}), 'copied_from': doc.id},
    )
    if doc.file:
        clone.file.save(name, ContentFile(_read_bytes(doc)), save=False)
    try:
        with transaction.atomic():
            clone.save()
    except Exception:
        if clone.file:
            clone.file.delete(save=False)
        raise
    return clone
