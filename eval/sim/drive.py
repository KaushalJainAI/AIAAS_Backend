"""
Simulated Drive, Sheets and Docs: the world's documents as fixture JSON.

Fixture schema (`fixtures['drive']`):

    {"files": [{"file_id": "f1", "name": "Budget Q3",
                "mime_type": "document", "content": "…"}],
     "sheets": {"ss1": {"tabs": {"Sheet1": [["sku", "qty"], ["a", "3"]]}}}}

A spreadsheet is a file with `mime_type: "spreadsheet"` **plus** its tabs in
`sheets` under the same id — ids are invented by the world author, and
`validate_world` refuses a sheet with no file. `mime_type` defaults to
`"text/plain"`; `"document"` behaves like a Google Doc for the docs tools.
Uploads, folders and sharing do not exist here: the world is flat on purpose,
so locating a file is one search, not a project.

Result shapes mirror `chat/tools/google/drive.py` and `docs.py`: file
summaries (`file_id, name, mime_type, modified, size, link, owners`), sheet
reads (`type: sheet_values` with `range, tabs, row_count, truncated,
values`), `{"status": "success", …}` for writes, and `"Error: …"` /
`{"error": …}` for bad calls. Drive ids are fixture-given; created rows are
`sim-file-N`.
"""
from __future__ import annotations

import copy
import json

TOOLS = (
    'drive_search_files',
    'drive_list_recent_files',
    'drive_get_file_metadata',
    'drive_read_file_content',
    'drive_create_file',
    'sheets_get_values',
    'sheets_update_values',
    'docs_read',
    'docs_create',
    'docs_append',
)

REQUIRED_FILE_KEYS = ('file_id', 'name', 'content')

_SUMMARY_KEYS = ('file_id', 'name', 'mime_type', 'modified', 'size', 'link', 'owners')


#: Fixture shorthand the generator writes (`"document"`, `"spreadsheet"`)
#: canonicalised to these — the tools only ever branch on the short forms.
_MIME_ALIASES = {
    'application/vnd.google-apps.document': 'document',
    'application/vnd.google-apps.spreadsheet': 'spreadsheet',
}


def _norm_file(raw: dict, index: int) -> dict:
    content = str(raw.get('content') or '')
    mime = str(raw.get('mime_type') or 'text/plain')
    mime = _MIME_ALIASES.get(mime, mime)
    return {
        'file_id': str(raw.get('file_id') or f'f{index}'),
        'name': str(raw.get('name') or f'file-{index}'),
        'mime_type': mime,
        'modified': str(raw.get('modified') or ''),
        'size': len(content.encode('utf-8')),
        'link': None,
        'owners': list(raw.get('owners') or []),
        'content': content,
    }


def _summary(f: dict) -> dict:
    return {k: f.get(k) for k in _SUMMARY_KEYS}


def _col_to_index(col: str) -> int:
    index = 0
    for char in col.upper():
        if 'A' <= char <= 'Z':
            index = index * 26 + (ord(char) - ord('A') + 1)
    return max(index - 1, 0)


def _parse_a1(range_: str) -> tuple[str | None, tuple | None]:
    """`'Tab!A1:B2'` -> (tab, (r1, c1, r2, c2)) with 0-based half-open bounds.

    Bare `'Tab'` or `''` selects the whole first matching tab; a bare cell
    range without a tab uses the first tab. Unparseable cells mean "no cell
    bounds" rather than an error — the range still names its tab.
    """
    text = str(range_ or '').strip().strip("'")
    tab: str | None = None
    cells: str = text
    if '!' in text:
        tab, cells = text.split('!', 1)
        tab, cells = tab.strip().strip("'"), cells.strip()
    bounds = None
    if ':' in cells:
        start, end = cells.split(':', 1)
        bounds = (_a1_cell(start) or (0, 0), _a1_cell(end) or (10 ** 9, 10 ** 9))
    elif cells:
        corner = _a1_cell(cells)
        if corner is not None:
            bounds = (corner, (corner[0] + 1, corner[1] + 1))
    if bounds is not None:
        (r1, c1), (r2, c2) = bounds
        bounds = (min(r1, r2), min(c1, c2), max(r1, r2), max(c1, c2))
    return (tab or None), bounds


def _a1_cell(cell: str) -> tuple[int, int] | None:
    letters = ''.join(c for c in cell if c.isalpha())
    digits = ''.join(c for c in cell if c.isdigit())
    if not letters or not digits:
        return None
    return (int(digits) - 1, _col_to_index(letters))


class DriveSim:
    """One attempt's Drive, Sheets and Docs. Reset per attempt, never shared."""

    TOOLS = TOOLS

    def __init__(self, fixtures: dict | None = None):
        self.reset(fixtures or {})

    def reset(self, fixtures: dict) -> None:
        self.files = [_norm_file(f, i) for i, f in
                      enumerate(fixtures.get('files') or [])]
        self.sheets = {str(sid): {
            'tabs': {str(tab): [list(map(str, row)) for row in rows]
                     for tab, rows in (spec.get('tabs') or {}).items()}}
            for sid, spec in (fixtures.get('sheets') or {}).items()
            if isinstance(spec, dict)}
        self.mutations: list[dict] = []
        self._seq = 0

    # -- dispatch ------------------------------------------------------

    def handles(self, name: str) -> bool:
        return name in TOOLS

    def run(self, name: str, args: dict) -> str:
        try:
            handler = getattr(self, f'_run_{name}', None)
            if handler is None:
                return f"Error: '{name}' is not simulated in this evaluation world."
            return handler(args or {})
        except Exception as exc:  # noqa: BLE001 - a simulator never raises
            return f'Error: {exc}'

    def _file(self, file_id: str) -> dict | None:
        return next((f for f in self.files if f['file_id'] == file_id), None)

    # -- drive reads -----------------------------------------------------

    def _run_drive_search_files(self, args: dict) -> str:
        query = str(args.get('query') or '').strip()
        if not query:
            return "Error: 'query' is required."
        needle = query.lower()
        try:
            limit = int(args.get('max_results') or 0) or 20
        except (TypeError, ValueError):
            limit = 20
        hits = [_summary(f) for f in self.files
                if needle in f['name'].lower() or needle in f['content'].lower()]
        hits = hits[:max(1, min(limit, 50))]
        return json.dumps({'type': 'drive_files', 'query': query,
                           'count': len(hits), 'files': hits})

    def _run_drive_list_recent_files(self, args: dict) -> str:
        try:
            limit = int(args.get('max_results') or 0) or 20
        except (TypeError, ValueError):
            limit = 20
        # Fixtures are oldest-first, so the tail is the most recent.
        files = [_summary(f) for f in reversed(self.files)]
        files = files[:max(1, min(limit, 50))]
        return json.dumps({'type': 'drive_files', 'count': len(files),
                           'files': files})

    def _run_drive_get_file_metadata(self, args: dict) -> str:
        file_id = str(args.get('file_id') or '').strip()
        if not file_id:
            return "Error: 'file_id' is required."
        found = self._file(file_id)
        if found is None:
            return json.dumps({'error': f'Unknown file {file_id}.',
                               'code': 'not_found'})
        return json.dumps({'type': 'drive_file', **_summary(found)})

    def _run_drive_read_file_content(self, args: dict) -> str:
        file_id = str(args.get('file_id') or '').strip()
        if not file_id:
            return "Error: 'file_id' is required."
        found = self._file(file_id)
        if found is None:
            return json.dumps({'error': f'Unknown file {file_id}.',
                               'code': 'not_found'})
        offset = args.get('offset') if isinstance(args.get('offset'), int) else 0
        offset = max(offset, 0)
        if found['mime_type'] == 'spreadsheet':
            tabs = (self.sheets.get(file_id) or {}).get('tabs') or {}
            first = next(iter(tabs), None)
            rows = tabs.get(first, []) if first else []
            text = '\n'.join(','.join(row) for row in rows)
            note = ("Only the first sheet is exported; use sheets_get_values "
                    "for other tabs.")
        else:
            text, note = found['content'], ''
        window = text[offset:offset + 15000]
        end = offset + len(window)
        return json.dumps({
            'type': 'drive_file_content', 'file_id': file_id,
            'name': found['name'], 'mime_type': found['mime_type'],
            'offset': offset, 'total_chars': len(text),
            'next_offset': end if end < len(text) else None,
            'note': note, 'content': window,
        })

    def _run_drive_create_file(self, args: dict) -> str:
        name = str(args.get('name') or '').strip()
        if not name:
            return "Error: 'name' is required."
        self._seq += 1
        mime = 'document' if args.get('as_google_doc') else 'text/plain'
        created = _norm_file({
            'file_id': f'sim-file-{self._seq}', 'name': name,
            'mime_type': mime, 'content': str(args.get('content') or '')}, 0)
        self.files.append(created)
        self.mutations.append({'created': created['file_id'], 'name': name})
        return json.dumps({'status': 'success', **_summary(created)})

    # -- sheets -----------------------------------------------------------

    def _sheet_tabs(self, spreadsheet_id: str) -> dict | None:
        return (self.sheets.get(spreadsheet_id) or {}).get('tabs')

    def _run_sheets_get_values(self, args: dict) -> str:
        spreadsheet_id = str(args.get('spreadsheet_id') or '').strip()
        if not spreadsheet_id:
            return "Error: 'spreadsheet_id' is required."
        tabs = self._sheet_tabs(spreadsheet_id)
        if tabs is None:
            return json.dumps({'error': f'Unknown spreadsheet {spreadsheet_id}.',
                               'code': 'not_found'})
        names = list(tabs)
        range_ = str(args.get('range') or '').strip()
        if not range_:
            if not names:
                return json.dumps({'type': 'sheet_values', 'tabs': [],
                                   'values': []})
            range_ = f"'{names[0]}'"
            values = tabs[names[0]]
            tab_names = names
        else:
            tab, bounds = _parse_a1(range_)
            if tab is not None and tab not in tabs:
                return json.dumps({'error': f'Unknown tab {tab}.',
                                   'code': 'not_found'})
            values = tabs[tab or names[0]] if names else []
            if bounds is not None:
                r1, c1, r2, c2 = bounds
                values = [row[c1:c2] for row in values[r1:r2]]
            tab_names = []
        limit = 100
        return json.dumps({
            'type': 'sheet_values',
            'range': range_,
            'tabs': tab_names,
            'row_count': len(values),
            'truncated': len(values) > limit,
            'values': values[:limit],
        })

    def _run_sheets_update_values(self, args: dict) -> str:
        spreadsheet_id = str(args.get('spreadsheet_id') or '').strip()
        range_ = str(args.get('range') or '').strip()
        values = args.get('values')
        if not spreadsheet_id or not range_ or not isinstance(values, list):
            return "Error: 'spreadsheet_id', 'range' and 'values' are required."
        tabs = self._sheet_tabs(spreadsheet_id)
        if tabs is None:
            return json.dumps({'error': f'Unknown spreadsheet {spreadsheet_id}.',
                               'code': 'not_found'})
        tab, bounds = _parse_a1(range_)
        names = list(tabs)
        if not names:
            return json.dumps({'error': f'{spreadsheet_id} has no tabs.',
                               'code': 'not_found'})
        target = tab if tab in tabs else names[0]
        grid = tabs[target]
        r1, c1, _, _ = bounds if bounds is not None else (0, 0, 10 ** 9, 10 ** 9)
        cells = 0
        for dr, row in enumerate(values):
            if not isinstance(row, list):
                continue
            while len(grid) <= r1 + dr:
                grid.append([])
            for dc, value in enumerate(row):
                while len(grid[r1 + dr]) <= c1 + dc:
                    grid[r1 + dr].append('')
                grid[r1 + dr][c1 + dc] = '' if value is None else str(value)
                cells += 1
        self.mutations.append({'cells': spreadsheet_id, 'range': range_,
                               'updated': cells})
        return json.dumps({'status': 'success', 'updated_range': range_,
                           'updated_cells': cells})

    # -- docs --------------------------------------------------------------

    def _doc(self, document_id: str) -> dict | None:
        found = self._file(document_id)
        if found is not None and found['mime_type'] != 'document':
            return None
        return found

    def _run_docs_read(self, args: dict) -> str:
        document_id = str(args.get('document_id') or '').strip()
        if not document_id:
            return json.dumps({'error': 'Give the document id.',
                               'code': 'tool_error'})
        found = self._doc(document_id)
        if found is None:
            return json.dumps({'error': f'Unknown document {document_id}.',
                               'code': 'not_found'})
        text = found['content']
        cap = 15000
        if len(text) > cap:
            text = text[:cap] + '\n[... clipped: the document continues in Docs]'
            return json.dumps({'type': 'docs_document', 'id': document_id,
                               'title': found['name'], 'text': text,
                               'truncated': True})
        return json.dumps({'type': 'docs_document', 'id': document_id,
                           'title': found['name'], 'text': text})

    def _run_docs_create(self, args: dict) -> str:
        title = str(args.get('title') or '').strip()
        if not title:
            return json.dumps({'error': 'Give the document a title.',
                               'code': 'tool_error'})
        self._seq += 1
        doc_id = f'sim-doc-{self._seq}'
        self.files.append(_norm_file(
            {'file_id': doc_id, 'name': title,
             'mime_type': 'application/vnd.google-apps.document',
             'content': ''}, 0))
        self.mutations.append({'created': doc_id, 'name': title})
        return json.dumps({
            'type': 'docs_document_created', 'id': doc_id, 'title': title,
            'url': f'https://docs.google.com/document/d/{doc_id}/edit'})

    def _run_docs_append(self, args: dict) -> str:
        document_id = str(args.get('document_id') or '').strip()
        text = str(args.get('text') or '')
        if not document_id or not text.strip():
            return json.dumps({'error': 'Give the document id and the text.',
                               'code': 'tool_error'})
        found = self._doc(document_id)
        if found is None:
            return json.dumps({'error': f'Unknown document {document_id}.',
                               'code': 'not_found'})
        paras = [p for p in text.split('\n\n') if p.strip()]
        body = '\n\n'.join(paras[:20])
        if len(paras) > 20:
            body += '\n\n[... 20 paragraphs appended; the rest refused: split it]'
        found['content'] = (found['content'] + '\n' + body) if found['content'] \
            else body
        found['size'] = len(found['content'].encode('utf-8'))
        self.mutations.append({'appended': document_id,
                               'characters': len(body)})
        return json.dumps({
            'type': 'docs_appended', 'id': document_id,
            'characters': len(body),
            'rendered': f'Appended {len(body):,} characters to the document.'})

    # -- grading ----------------------------------------------------------

    def snapshot(self) -> dict:
        # Content ships too: `env_file(contains=)` grades what a created
        # file holds, and fixtures are small by construction (caps in
        # `validate_world`). Agent-written files ride the same map.
        return {
            'files': [{'file_id': f['file_id'], 'name': f['name'],
                       'mime_type': f['mime_type'],
                       'content': f['content'][:15000]} for f in self.files],
            'sheets': copy.deepcopy(self.sheets),
        }

    def changes(self) -> dict:
        out: dict = {}
        created = [m for m in self.mutations if 'created' in m][:20]
        if created:
            out['files_created'] = [
                {'name': m['name'], 'file_id': m['created']} for m in created]
        cells = [m for m in self.mutations if 'cells' in m][:20]
        if cells:
            out['cells_updated'] = cells
        appended = [m for m in self.mutations if 'appended' in m][:20]
        if appended:
            out['docs_appended'] = appended
        return out

    def apply_expected(self, expect: dict) -> None:
        """Perform the case's expected drive writes (generation-time proof)."""
        for item in (expect or {}).get('drive_files') or []:
            if isinstance(item, dict) and item.get('name') is not None:
                self._run_drive_create_file(dict(item))
        for item in (expect or {}).get('drive_cells') or []:
            if isinstance(item, dict):
                self._run_sheets_update_values(dict(item))


__all__ = ['TOOLS', 'DriveSim', 'REQUIRED_FILE_KEYS']
