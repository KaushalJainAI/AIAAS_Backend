"""
Notion over its REST API, as native connector tools.

Why native instead of the MCP server: the curated `@notionhq/notion-mcp-server`
row is stdio, and production runs no Node (`MCP_ALLOW_STDIO=False`), so the
card could never start there. Notion's API needs only a bearer token from an
internal integration — no subprocess, no per-connector memory — so these tools
call it from this process, governed by the same Connections card (`native`
row, `notion` credential) that used to front the subprocess.

Three reads and one write. Reads are `effect="read"` and parallel-safe;
creating a page is `sensitive` + `irreversible` and always stops for a human.
Failures carry a code the model can act on (`credential_missing` means connect
Notion on the Connections page), matching the shape connector errors use.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict

from tools_config.overlay import alimit

from .registry import tool

logger = logging.getLogger(__name__)

CONNECTOR = "notion"
CREDENTIAL_SLUG = "notion"
_API = "https://api.notion.com/v1"
_VERSION = "2022-06-28"
HINT = "Ask the user to connect Notion on the Connections page."

#: The most page text one read returns. A clipped body says so and names the
#: omission — a truncated read and a short one must not look alike.
MAX_CHARS = 15000
MAX_ROWS = 50


class NotionAPIError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message

    def as_json(self) -> str:
        return json.dumps({"error": self.message, "code": self.code})


async def _token(user_id: int) -> str:
    from credentials.manager import CredentialManager
    from asgiref.sync import sync_to_async

    try:
        credential = await sync_to_async(CredentialManager.lookup_by_slug_sync)(
            CREDENTIAL_SLUG, user_id)
        data = (credential.get_credential_data() or {}) if credential else {}
    except Exception:  # noqa: BLE001
        data = {}
    token = (data or {}).get('token')
    if not token:
        raise NotionAPIError(
            'credential_missing',
            f'Notion is not connected: {HINT}')
    return str(token)


async def _call(user_id: int, method: str, path: str,
                payload: dict | None = None) -> dict:
    from workflow_backend.httpclient import shared_client

    token = await _token(user_id)
    try:
        resp = await shared_client().request(
            method, f'{_API}{path}',
            headers={'Authorization': f'Bearer {token}',
                     'Notion-Version': _VERSION},
            json=payload, timeout=20,
        )
    except Exception as exc:  # noqa: BLE001
        raise NotionAPIError('tool_error',
                             'Notion could not be reached. Try again shortly.') from exc
    if resp.status_code == 401:
        raise NotionAPIError(
            'credential_missing',
            f'Notion refused the token. Check it on the Connections page: {HINT}')
    if resp.status_code == 429:
        raise NotionAPIError('tool_error',
                             'Notion is rate-limited. Try again later.')
    if resp.status_code == 404:
        raise NotionAPIError('not_found',
                             'Notion has no such page or database — or it was never '
                             'shared with the integration. Share it from Notion first.')
    if resp.status_code >= 400:
        detail = ''
        try:
            detail = str(resp.json().get('message') or '')
        except ValueError:
            detail = ''
        raise NotionAPIError('tool_error',
                             f'Notion refused the call ({resp.status_code}). {detail}'.strip())
    try:
        return resp.json()
    except ValueError as exc:
        raise NotionAPIError('tool_error',
                             'Notion returned something unreadable.') from exc


def _rich_text(rich: list) -> str:
    return ''.join(str(r.get('plain_text') or '') for r in rich or [])


def _page_title(obj: dict) -> str:
    if obj.get('object') == 'database':
        return _rich_text(obj.get('title') or [])
    for prop in (obj.get('properties') or {}).values():
        if isinstance(prop, dict) and prop.get('type') == 'title':
            return _rich_text(prop.get('title') or [])
    return ''


def _block_text(block: dict) -> str:
    kind = block.get('type') or ''
    inner = block.get(kind) or {}
    if isinstance(inner, dict) and 'rich_text' in inner:
        return _rich_text(inner.get('rich_text') or [])
    return ''


def _prop_value(prop: dict) -> Any:
    kind = (prop or {}).get('type')
    inner = (prop or {}).get(kind) if kind else None
    if kind in ('title', 'rich_text'):
        return _rich_text((inner or []) if isinstance(inner, list) else [])
    if kind == 'number':
        return inner
    if kind == 'select':
        return (inner or {}).get('name') if isinstance(inner, dict) else None
    if kind == 'multi_select':
        return [o.get('name') for o in (inner or []) if isinstance(o, dict)]
    if kind == 'date':
        return (inner or {}).get('start') if isinstance(inner, dict) else None
    if kind == 'checkbox':
        return bool(inner)
    if kind in ('url', 'email', 'phone_number'):
        return inner
    if kind == 'status':
        return (inner or {}).get('name') if isinstance(inner, dict) else None
    if kind == 'people':
        return [u.get('name') for u in (inner or []) if isinstance(u, dict)]
    return None


@tool({
    'type': 'function',
    'function': {
        'name': 'notion_search',
        'description': (
            '[Notion] Search the pages and databases shared with the '
            'integration, by title. Returns id, kind, title and URL for each. '
            'An empty query lists what was touched most recently.'
        ),
        'parameters': {
            'type': 'object',
            'properties': {
                'query': {'type': 'string', 'description': 'Title words to match.'},
                'max_results': {'type': 'integer', 'description': 'How many (default from settings).'},
            },
            'additionalProperties': False,
        },
    },
}, parallel=True, effect='read', connector=CONNECTOR)
async def notion_search(args: Dict, context: Dict) -> str:
    user_id = context.get('user_id')
    if not user_id:
        return json.dumps({'error': 'No user context.', 'code': 'tool_error'})
    try:
        cap = await alimit(context, 'notion_search', 'maxResults')
        requested = args.get('max_results')
        limit = min(int(requested), cap) if isinstance(requested, int) and requested > 0 else cap
        payload: dict[str, Any] = {'page_size': limit}
        if (args.get('query') or '').strip():
            payload['query'] = args['query'].strip()
        found = await _call(user_id, 'POST', '/search', payload)
    except NotionAPIError as exc:
        return exc.as_json()
    out = []
    for obj in found.get('results') or []:
        if not isinstance(obj, dict):
            continue
        out.append({
            'id': obj.get('id'), 'kind': obj.get('object'),
            'title': _page_title(obj), 'url': obj.get('url'),
        })
    return json.dumps({'type': 'notion_search', 'count': len(out), 'results': out})


@tool({
    'type': 'function',
    'function': {
        'name': 'notion_read_page',
        'description': (
            '[Notion] Read a page: its title, properties and block text. '
            'Long pages are clipped with a notice, never silently.'
        ),
        'parameters': {
            'type': 'object',
            'properties': {
                'page_id': {'type': 'string', 'description': 'The page id (dashes or not).'},
            },
            'required': ['page_id'],
            'additionalProperties': False,
        },
    },
}, parallel=True, effect='read', connector=CONNECTOR)
async def notion_read_page(args: Dict, context: Dict) -> str:
    user_id = context.get('user_id')
    if not user_id:
        return json.dumps({'error': 'No user context.', 'code': 'tool_error'})
    page_id = str(args.get('page_id') or '').strip()
    if not page_id:
        return json.dumps({'error': 'Give the page id.', 'code': 'tool_error'})
    try:
        page = await _call(user_id, 'GET', f'/pages/{page_id}')
        blocks = await _call(user_id, 'GET', f'/blocks/{page_id}/children',
                             {'page_size': 100})
    except NotionAPIError as exc:
        return exc.as_json()
    texts = [t for b in blocks.get('results') or []
             if isinstance(b, dict) and (t := _block_text(b))]
    body = '\n'.join(texts)
    truncated = False
    if len(body) > MAX_CHARS:
        body = body[:MAX_CHARS] + '\n[... clipped: page continues in Notion]'
        truncated = True
    props = {k: _prop_value(v) for k, v in (page.get('properties') or {}).items()}
    return json.dumps({
        'type': 'notion_page', 'id': page.get('id'),
        'title': _page_title(page), 'url': page.get('url'),
        'properties': props, 'text': body,
        **({'truncated': True} if truncated else {}),
    })


@tool({
    'type': 'function',
    'function': {
        'name': 'notion_query_database',
        'description': (
            '[Notion] Read the rows of a database shared with the integration. '
            'Returns up to 50 rows with plain values; use search to find the id.'
        ),
        'parameters': {
            'type': 'object',
            'properties': {
                'database_id': {'type': 'string', 'description': 'The database id.'},
                'max_rows': {'type': 'integer', 'description': 'How many rows (default 50).'},
            },
            'required': ['database_id'],
            'additionalProperties': False,
        },
    },
}, parallel=True, effect='read', connector=CONNECTOR)
async def notion_query_database(args: Dict, context: Dict) -> str:
    user_id = context.get('user_id')
    if not user_id:
        return json.dumps({'error': 'No user context.', 'code': 'tool_error'})
    database_id = str(args.get('database_id') or '').strip()
    if not database_id:
        return json.dumps({'error': 'Give the database id.', 'code': 'tool_error'})
    try:
        requested = args.get('max_rows')
        limit = min(int(requested), MAX_ROWS) if isinstance(requested, int) and requested > 0 else MAX_ROWS
        found = await _call(user_id, 'POST', f'/databases/{database_id}/query',
                            {'page_size': limit})
    except NotionAPIError as exc:
        return exc.as_json()
    rows = []
    for obj in found.get('results') or []:
        if not isinstance(obj, dict):
            continue
        rows.append({
            'id': obj.get('id'),
            'values': {k: _prop_value(v) for k, v in (obj.get('properties') or {}).items()},
        })
    return json.dumps({'type': 'notion_rows', 'count': len(rows), 'rows': rows})


@tool({
    'type': 'function',
    'function': {
        'name': 'notion_create_page',
        'description': (
            '[Notion] Create a page under a parent page, with a title and '
            'text. The user approves each page. Database rows are refused — '
            'their schema is per-database, so name the database and ask what '
            'goes in each field instead of guessing.'
        ),
        'parameters': {
            'type': 'object',
            'properties': {
                'parent_page_id': {'type': 'string', 'description': 'The parent page id.'},
                'title': {'type': 'string', 'description': 'The page title.'},
                'text': {'type': 'string', 'description': 'Body text, paragraphs split on blank lines.'},
            },
            'required': ['parent_page_id', 'title'],
            'additionalProperties': False,
        },
    },
}, sensitive=True, effect='irreversible', connector=CONNECTOR)
async def notion_create_page(args: Dict, context: Dict) -> str:
    user_id = context.get('user_id')
    if not user_id:
        return json.dumps({'error': 'No user context.', 'code': 'tool_error'})
    parent = str(args.get('parent_page_id') or '').strip()
    title = str(args.get('title') or '').strip()
    if not parent or not title:
        return json.dumps({'error': 'Give the parent page id and a title.',
                           'code': 'tool_error'})
    paras = [p.strip() for p in str(args.get('text') or '').split('\n\n') if p.strip()]
    try:
        made = await _call(user_id, 'POST', '/pages', {
            'parent': {'page_id': parent},
            'properties': {'title': {'title': [{'text': {'content': title}}]}},
            'children': [
                {'object': 'block', 'type': 'paragraph',
                 'paragraph': {'rich_text': [{'type': 'text', 'text': {'content': p[:2000]}}]}}
                for p in paras[:20]
            ],
        })
    except NotionAPIError as exc:
        return exc.as_json()
    return json.dumps({'type': 'notion_page_created', 'id': made.get('id'),
                       'url': made.get('url')})
