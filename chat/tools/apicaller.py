"""
Any HTTP API, through one generic caller instead of a connector per system.

`list_api_operations` searches the connection's operation index (never the
whole 2 MB spec); `call_api` validates method and path against the spec when
there is one — an unknown path is refused with the three nearest operations —
and always against the egress guard and the method allowlist. Auth is
injected at dispatch from the vault reference; any `Authorization` header the
model supplies is dropped. `GET`/`HEAD` read; everything else pauses for a
human. `apiConnections` says which connections are in play, each in `read`
or `all` mode — empty means none.
"""
from __future__ import annotations

import difflib
import json
import logging
from typing import Dict
from urllib.parse import urljoin, urlparse

from asgiref.sync import sync_to_async

from workflow_backend.thresholds import TOOL_OUTPUT_CHAR_LIMIT

from .registry import tool

from tools_config.overlay import alimit

from tools_config.settings_schema import (
    _API_OP_LIST_LIMIT,
    _API_RESPONSE_CHARS,
)

logger = logging.getLogger(__name__)

READ_METHODS = frozenset({'GET', 'HEAD'})
#: Now workspace knobs (`list_api_operations.maxOps` / `call_api.responseChars`);
#: the constants stay as the floor under a failed overlay read.
OPERATION_LIST_LIMIT = 30
RESPONSE_CHARS = 32_000


class _Refuse(ValueError):
    """A use error: rendered as the tool's answer, not a traceback."""


def _scope(context: Dict) -> dict | None:
    """This run's API scope, or None for any the user owns.

    None is unrestricted: an agent built before scopes never had one applied,
    and turning enforcement on must not empty its reach. Empty means none —
    the field arrives with the feature, so no existing agent holds it.
    """
    raw = context.get('api_connections')
    if raw is None:
        return None
    out = {}
    if isinstance(raw, dict):
        items = raw.items()
    else:
        items = [(v, 'all') for v in (raw or [])]
    for key, mode in items:
        try:
            out[int(key)] = 'read' if str(mode).lower() == 'read' else 'all'
        except (TypeError, ValueError):
            continue
    return out


async def _connection(user_id: int, connection_id: int, context: Dict):
    from data.models import ApiConnection

    try:
        wanted = int(connection_id)
    except (TypeError, ValueError):
        return None, 'all', 'Give the numeric id of a connection from list_api_operations.'
    row = await ApiConnection.objects.filter(id=wanted, user_id=user_id).afirst()
    if row is None:
        return None, 'all', f'No API connection {wanted} belongs to this user.'
    scope = _scope(context)
    if scope is None:
        return row, 'all', ''
    if wanted not in scope:
        return None, 'all', (
            f'Connection "{row.name}" is not selected for this run. Ask the '
            'user to select it in the agent\'s settings.')
    return row, scope[wanted], ''


def _operations(spec: dict) -> list[dict]:
    paths = (spec or {}).get('paths') or {}
    ops = []
    if isinstance(paths, dict):
        for path, item in paths.items():
            if not isinstance(item, dict):
                continue
            for method, op in item.items():
                if method.lower() not in (
                        'get', 'post', 'put', 'patch', 'delete', 'head', 'options'):
                    continue
                op = op if isinstance(op, dict) else {}
                ops.append({
                    'method': method.upper(), 'path': path,
                    'summary': str(op.get('summary') or '')[:160],
                    'operation_id': str(op.get('operationId') or ''),
                    'tags': [str(t) for t in (op.get('tags') or [])][:5],
                })
    return ops


def _nearest(ops: list[dict], method: str, path: str) -> list[str]:
    candidates = [f"{o['method']} {o['path']}" for o in ops]
    return difflib.get_close_matches(f'{method} {path}', candidates, n=3)


@tool({
    'type': 'function',
    'function': {
        'name': 'list_api_operations',
        'description': (
            'Search an API connection\'s operations by path, summary or tag. '
            'Returns compact signatures, never the whole spec — specs are '
            'routinely megabytes.'
        ),
        'parameters': {
            'type': 'object',
            'properties': {
                'connection': {'type': 'integer', 'description': 'Connection id.'},
                'query': {'type': 'string', 'description': 'Words to match.'},
            },
            'required': ['connection'],
            'additionalProperties': False,
        },
    },
}, effect='read')
async def list_api_operations(args: Dict, context: Dict) -> str:
    user_id = context.get('user_id')
    if not user_id:
        return json.dumps({'error': 'No user context.'})
    try:
        from data.models import ApiConnection

        rows = [row async for row in ApiConnection.objects.filter(
            user_id=user_id).order_by('name')]
    except Exception:
        logger.exception('[API] list failed')
        return json.dumps({'error': 'The connections could not be listed.'})
    query = str(args.get('query') or '').lower()
    wanted = args.get('connection')
    out = []
    for row in rows:
        if wanted is not None:
            try:
                if row.id != int(wanted):
                    continue
            except (TypeError, ValueError):
                return json.dumps({'error': '`connection` must be numeric.'})
        ops = _operations(row.openapi_spec or {})
        if query:
            ops = [o for o in ops
                   if query in (o['path'] + ' ' + o['summary'] + ' '
                                + ' '.join(o['tags'])).lower()]
        limited = ops[:await alimit(context, 'list_api_operations', 'maxOps')]
        out.append({'id': row.id, 'name': row.name, 'base_url': row.base_url,
                    'operations': limited,
                    **({'truncated': True} if len(ops) > len(limited) else {})})
    return json.dumps({'connections': out}, default=str)


@tool({
    'type': 'function',
    'function': {
        'name': 'call_api',
        'description': (
            'Call one operation on an API connection. GET and HEAD read; '
            'anything else pauses for a human. The call is checked against '
            'the connection\'s spec when it has one, and always against the '
            'egress guard and the allowed methods. Auth comes from the vault; '
            'never send credentials in headers or the body.'
        ),
        'parameters': {
            'type': 'object',
            'properties': {
                'connection': {'type': 'integer', 'description': 'Connection id.'},
                'method': {'type': 'string', 'description': 'GET, POST, PUT, PATCH or DELETE.'},
                'path': {'type': 'string', 'description': 'Operation path, e.g. /v1/orders.'},
                'query': {'type': 'object', 'description': 'Query parameters.',
                          'additionalProperties': True},
                'body': {'description': 'JSON body for writes.'},
            },
            'required': ['connection', 'method', 'path'],
            'additionalProperties': False,
        },
    },
}, sensitive=True, effect='irreversible')
async def call_api(args: Dict, context: Dict) -> str:
    user_id = context.get('user_id')
    if not user_id:
        return json.dumps({'error': 'No user context.'})
    row, mode, error = await _connection(user_id, args.get('connection'), context)
    if row is None:
        return json.dumps({'error': error})
    method = str(args.get('method') or '').strip().upper()
    path = str(args.get('path') or '').strip()
    if not path.startswith('/'):
        return json.dumps({'error': 'Give the operation path starting with `/`.'})
    allowed = [m.upper() for m in (row.allowed_methods or [])] or ['GET', 'HEAD']
    if method not in allowed:
        return json.dumps({
            'error': f'{method} is not allowed on "{row.name}". Allowed: {", ".join(allowed)}.'})
    if mode == 'read' and method not in READ_METHODS:
        return json.dumps({
            'error': f'"{row.name}" is read-only for this run; {method} needs '
                     'the connection in full mode.'})
    ops = _operations(row.openapi_spec or {})
    if ops and not any(o['method'] == method and o['path'] == path for o in ops):
        nearest = _nearest(ops, method, path)
        return json.dumps({
            'error': f'No {method} {path} on "{row.name}".' + (
                f' Nearest: {", ".join(nearest)}.' if nearest else '')})

    from core.safety.net import check_egress

    target = urljoin(row.base_url.rstrip('/') + '/', path.lstrip('/'))
    # The connection's own host is allowed by virtue of being configured;
    # `apiHosts` adds to it (subdomains, a second region). SSRF always applies.
    hosts = {urlparse(row.base_url).hostname or ''}
    hosts.update(str(h) for h in (context.get('api_hosts') or []) if str(h).strip())
    hosts.discard('')
    ok, reason = await _check(target, sorted(hosts))
    if not ok:
        return json.dumps({'error': reason})

    query = args.get('query') or {}
    if not isinstance(query, dict):
        return json.dumps({'error': '`query` must be an object.'})
    headers, params = await _auth_headers(row, user_id, dict(query))
    try:
        from workflow_backend.httpclient import shared_client

        client = shared_client()
        if method == 'GET':
            resp = await client.get(target, headers=headers, params=params, timeout=30)
        elif method == 'HEAD':
            resp = await client.request('HEAD', target, headers=headers,
                                        params=params, timeout=30)
        elif method == 'DELETE':
            resp = await client.request(
                'DELETE', target, headers=headers, params=params,
                json=args.get('body'), timeout=30)
        else:
            resp = await client.request(
                method, target, headers=headers, params=params,
                json=args.get('body'), timeout=30)
    except Exception:  # noqa: BLE001
        logger.exception('[API] call failed')
        return json.dumps({'error': 'The API could not be reached.'})
    if resp.status_code == 429:
        return json.dumps({'error': 'The API is rate-limited. Try again later.'})
    if resp.status_code >= 400:
        return json.dumps({'error': f'The API refused the call ({resp.status_code}).'})
    content_type = (resp.headers.get('content-type') or '').lower()
    if 'octet-stream' in content_type or 'pdf' in content_type or 'zip' in content_type:
        return await _save_binary(context, row, path, bytes(resp.content), content_type)
    try:
        payload = resp.json()
    except ValueError:
        payload = {'text': resp.text[:await alimit(context, 'call_api', 'responseChars')]}
    text = json.dumps(payload, default=str)
    if len(text) > TOOL_OUTPUT_CHAR_LIMIT:
        return await _save_text(context, row, path, text)
    out = {'status': resp.status_code, 'data': payload}
    if method not in READ_METHODS:
        out['rendered'] = f'{method} {path} answered {resp.status_code}.'
    return json.dumps(out, default=str)


async def _check(target: str, hosts: list) -> tuple[bool, str]:
    from core.safety.net import check_egress

    host = urlparse(target).hostname or ''
    scope = {'apiHosts': hosts} if hosts else {}
    return check_egress(target, scope) if '://' in target else (False, 'Bad URL.')


async def _auth_headers(row, user_id: int, query: dict) -> tuple[dict, dict]:
    """Auth from the vault reference. A model-supplied `Authorization` is
    dropped — the model names which credential to use by configuring the
    connection, never by handing over a value."""
    from credentials.refs import aresolve_refs

    headers = {'User-Agent': 'AIAAS/1.0'}
    auth = row.auth or {}
    kind = str(auth.get('type') or 'bearer').lower()
    ref = str(auth.get('secret_ref') or '').strip()
    params = dict(query)
    params.pop('Authorization', None)
    if not ref:
        return headers, params
    try:
        resolved = await aresolve_refs({'v': {'secret_ref': ref}}, user_id, _slugs(ref))
    except Exception:  # noqa: BLE001
        logger.exception('[API] Auth resolution failed')
        return headers, params
    value = str(resolved.get('v') or '')
    if not value:
        return headers, params
    if kind == 'bearer':
        headers['Authorization'] = f'Bearer {value}'
    elif kind == 'basic':
        import base64 as _b64

        headers['Authorization'] = 'Basic ' + _b64.b64encode(value.encode()).decode()
    elif kind == 'query':
        params[str(auth.get('param') or 'api_key')] = value
    else:
        headers[str(auth.get('header') or 'X-API-Key')] = value
    return headers, params


def _slugs(ref: str) -> set[str]:
    import re as _re

    match = _re.fullmatch(r'\s*([A-Za-z0-9][A-Za-z0-9_-]*)\.[A-Za-z0-9_]+\s*', ref)
    return {match.group(1)} if match else set()


async def _save_binary(context: Dict, row, path: str, data: bytes, content_type: str) -> str:
    from inference import vfs as _vfs

    scope = context.get('file_scope')
    if scope is None or not data:
        return json.dumps({'error': 'The API returned a file, but there is nowhere to keep it.'})
    name = path.rstrip('/').rsplit('/', 1)[-1] or 'download'
    prefix = '/' + '/'.join(scope.write_prefix) + '/' if scope.write_prefix else '/'
    try:
        out = await sync_to_async(_vfs.write_binary)(
            scope, f'{prefix}api/{name}', bytes(data),
            text=f'Response from {row.name} {path} ({content_type}).')
    except Exception as exc:  # noqa: BLE001
        from inference.vfs import VfsError

        if isinstance(exc, VfsError):
            return json.dumps({'error': str(exc)})
        logger.exception('[API] Could not save response')
        return json.dumps({'error': 'The response could not be saved.'})
    return json.dumps({'status': 200, 'saved_path': out['path']})


async def _save_text(context: Dict, row, path: str, text: str) -> str:
    from inference import vfs as _vfs

    scope = context.get('file_scope')
    if scope is None:
        return json.dumps({'status': 200, 'data_truncated': True,
                           'note': 'The response was too large for one answer.'})
    name = (path.rstrip('/').rsplit('/', 1)[-1] or 'response') + '.json'
    prefix = '/' + '/'.join(scope.write_prefix) + '/' if scope.write_prefix else '/'
    try:
        out = await sync_to_async(_vfs.write_file)(
            scope, f'{prefix}api/{name}', text[:200_000])
    except Exception as exc:  # noqa: BLE001
        from inference.vfs import VfsError

        if isinstance(exc, VfsError):
            return json.dumps({'error': str(exc)})
        logger.exception('[API] Could not save response')
        return json.dumps({'error': 'The response could not be saved.'})
    return json.dumps({'status': 200, 'saved_path': out['path'],
                       'note': 'The full response is in the file.'})


API_TOOLS = ('list_api_operations', 'call_api')
