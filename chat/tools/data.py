"""
The user's databases, reached generically instead of one connector per system.

`list_data_connections` and `describe_schema` read; `query_sql` reads rows
(SELECT / WITH / VALUES / EXPLAIN only, parsed — never regex — inside a
read-only transaction with a 30 s timeout, 1,000 rows inline and the rest as
a CSV in the caller's write folder); `execute_sql` writes, only on a
connection whose owner allowed it, and pauses for a human showing the
statement. Hosts go through the P0 egress guard, and `dataConnections` says
which connections are in play — empty means none.
"""
from __future__ import annotations

import csv
import io
import json
import logging
from typing import Dict

from asgiref.sync import sync_to_async

from workflow_backend.thresholds import TOOL_OUTPUT_CHAR_LIMIT

from .registry import tool

from tools_config.overlay import alimit

from tools_config.settings_schema import _SQL_ROW_CAP

logger = logging.getLogger(__name__)


def _scope_ids(context: Dict) -> list[int] | None:
    raw = context.get('data_connections')
    if raw is None:
        return None
    return [int(v) for v in raw if isinstance(v, int) and not isinstance(v, bool)]


async def _connection(user_id: int, connection_id: int, context: Dict):
    from data.models import DataConnection

    try:
        wanted = int(connection_id)
    except (TypeError, ValueError):
        return None, 'Give the numeric id of a connection from list_data_connections.'
    row = await DataConnection.objects.filter(id=wanted, user_id=user_id).afirst()
    if row is None:
        return None, f'No data connection {wanted} belongs to this user.'
    scope = _scope_ids(context)
    if scope is not None and row.id not in scope:
        return None, (
            f'Connection "{row.name}" is not selected for this run. Ask the '
            'user to select it in the agent\'s settings.')
    return row, ''


def _present(connections) -> list[dict]:
    from data.drivers import available_kinds

    kinds = set(available_kinds())
    out = []
    for row in connections:
        out.append({
            'id': row.id, 'name': row.name, 'kind': row.kind,
            'database': row.database or None,
            'allow_write': row.allow_write,
            **({} if row.kind in kinds else {
                'unavailable': f'{row.kind} needs a driver this platform does not carry.'}),
        })
    return out


async def _list_rows(user_id: int):
    from data.models import DataConnection

    return [row async for row in DataConnection.objects.filter(
        user_id=user_id).order_by('name')]


@tool({
    'type': 'function',
    'function': {
        'name': 'list_data_connections',
        'description': (
            'List the databases this user connected: Postgres, MySQL, BigQuery '
            'and SQLite files in their workspace. A connection the run did not '
            'select is listed but refused when used.'
        ),
        'parameters': {'type': 'object', 'properties': {}, 'additionalProperties': False},
    },
}, effect='read')
async def list_data_connections(args: Dict, context: Dict) -> str:
    user_id = context.get('user_id')
    if not user_id:
        return json.dumps({'error': 'No user context.'})
    try:
        rows = await _list_rows(user_id)
    except Exception:
        logger.exception('[Data] list_data_connections failed')
        return json.dumps({'error': 'The connections could not be listed.'})
    return json.dumps({'connections': _present(rows)}, default=str)


@tool({
    'type': 'function',
    'function': {
        'name': 'describe_schema',
        'description': (
            'Tables of a connection, or one table\'s columns. Cached briefly. '
            'Read this before querying a database you have not seen.'
        ),
        'parameters': {
            'type': 'object',
            'properties': {
                'connection': {'type': 'integer', 'description': 'Connection id.'},
                'table': {'type': 'string', 'description': 'One table, or omit for all.'},
            },
            'required': ['connection'],
            'additionalProperties': False,
        },
    },
}, effect='read')
async def describe_schema(args: Dict, context: Dict) -> str:
    from data import drivers

    user_id = context.get('user_id')
    if not user_id:
        return json.dumps({'error': 'No user context.'})
    row, error = await _connection(user_id, args.get('connection'), context)
    if row is None:
        return json.dumps({'error': error})
    table = str(args.get('table') or '').strip()[:128]
    try:
        out = await sync_to_async(drivers.describe)(
            row, table=table, user_id=user_id,
            file_scope=context.get('file_scope'),
            scope_hosts=_egress_hosts(context))
    except drivers.DataError as exc:
        return json.dumps({'error': str(exc)})
    except Exception:
        logger.exception('[Data] describe_schema failed')
        return json.dumps({'error': 'The schema could not be read.'})
    return json.dumps(out, default=str)


def _egress_hosts(context: Dict) -> list:
    raw = context.get('db_hosts') or []
    return [str(h) for h in raw if str(h).strip()]


@tool({
    'type': 'function',
    'function': {
        'name': 'query_sql',
        'description': (
            'Read rows with SQL: SELECT / WITH / VALUES / EXPLAIN only, one '
            'statement, inside a read-only transaction with a 30 s timeout. '
            'At most 1,000 rows come back inline; more are written to a CSV '
            'in your files and the path comes back instead. Writes, DDL and '
            'chained statements are refused with the reason.'
        ),
        'parameters': {
            'type': 'object',
            'properties': {
                'connection': {'type': 'integer', 'description': 'Connection id.'},
                'sql': {'type': 'string', 'description': 'The SELECT to run.'},
                'params': {'type': 'array',
                           'description': 'Bound parameters, in order.',
                           'items': {}},
            },
            'required': ['connection', 'sql'],
            'additionalProperties': False,
        },
    },
}, effect='read')
async def query_sql(args: Dict, context: Dict) -> str:
    from data import drivers

    user_id = context.get('user_id')
    if not user_id:
        return json.dumps({'error': 'No user context.'})
    row, error = await _connection(user_id, args.get('connection'), context)
    if row is None:
        return json.dumps({'error': error})
    sql = str(args.get('sql') or '')
    params = args.get('params') or []
    if not isinstance(params, list):
        return json.dumps({'error': '`params` must be a list.'})
    try:
        out = await sync_to_async(drivers.run)(
            row, sql, params, user_id=user_id,
            file_scope=context.get('file_scope'),
            scope_hosts=_egress_hosts(context),
            row_cap=await alimit(context, 'query_sql', 'maxRows'))
    except (drivers.DataError, Exception) as exc:
        from data.drivers import DataError

        if isinstance(exc, DataError):
            return json.dumps({'error': str(exc)})
        logger.exception('[Data] query_sql failed')
        return json.dumps({'error': 'The query failed.'})
    rows = out.get('rows') or []
    if out.get('more'):
        # More rows than fit inline: fetch the whole answer (guarded) and
        # spill it, so the count the model reports is the true one.
        try:
            out = await sync_to_async(drivers.run)(
                row, sql, params, user_id=user_id,
                file_scope=context.get('file_scope'),
                scope_hosts=_egress_hosts(context), full=True)
        except (drivers.DataError, Exception) as exc:
            from data.drivers import DataError

            if isinstance(exc, DataError):
                return json.dumps({'error': str(exc)})
            logger.exception('[Data] query_sql full fetch failed')
            return json.dumps({'error': 'The query failed.'})
        return await _spill_csv(context, row, out)
    row_cap = await alimit(context, 'query_sql', 'maxRows')
    if len(rows) > row_cap:
        return await _spill_csv(context, row, out)
    text = json.dumps(out, default=str)
    if len(text) > TOOL_OUTPUT_CHAR_LIMIT:
        return await _spill_csv(context, row, out)
    return json.dumps({**out, 'row_count': len(rows)}, default=str)


async def _spill_csv(context: Dict, connection, out: dict) -> str:
    """Rows past the inline cap land as a CSV in the write folder."""
    from inference import vfs as _vfs

    scope = context.get('file_scope')
    if scope is None:
        limited = {**out, 'rows': (out.get('rows') or [])[:100],
                   'row_count': len(out.get('rows') or []),
                   'truncated': True,
                   'note': 'Too many rows for one answer; showing the first 100.'}
        return json.dumps(limited, default=str)
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(out.get('columns') or [])
    for record in out.get('rows') or []:
        writer.writerow(['' if v is None else str(v) for v in record])
    name = f'query-{connection.id}.csv'
    prefix = '/' + '/'.join(scope.write_prefix) + '/' if scope.write_prefix else '/'
    try:
        saved = await sync_to_async(_vfs.write_file)(
            scope, f'{prefix}queries/{name}', buf.getvalue())
    except Exception as exc:  # noqa: BLE001
        from inference.vfs import VfsError

        if isinstance(exc, VfsError):
            return json.dumps({'error': str(exc)})
        logger.exception('[Data] Could not spill CSV')
        return json.dumps({'error': 'The result could not be saved.'})
    return json.dumps({
        'row_count': len(out.get('rows') or []),
        'csv_path': saved['path'],
        'rendered': f'{len(out.get("rows") or []):,} rows saved to {saved["path"]}.',
    })


@tool({
    'type': 'function',
    'function': {
        'name': 'execute_sql',
        'description': (
            'Write with SQL on a connection whose owner allowed writes. The '
            'statement and an affected-row estimate show on the approval card. '
            'One statement; schema and server work is refused.'
        ),
        'parameters': {
            'type': 'object',
            'properties': {
                'connection': {'type': 'integer', 'description': 'Connection id.'},
                'sql': {'type': 'string', 'description': 'The INSERT/UPDATE/DELETE to run.'},
                'params': {'type': 'array',
                           'description': 'Bound parameters, in order.',
                           'items': {}},
            },
            'required': ['connection', 'sql'],
            'additionalProperties': False,
        },
    },
}, sensitive=True, effect='irreversible')
async def execute_sql(args: Dict, context: Dict) -> str:
    from data import drivers

    user_id = context.get('user_id')
    if not user_id:
        return json.dumps({'error': 'No user context.'})
    row, error = await _connection(user_id, args.get('connection'), context)
    if row is None:
        return json.dumps({'error': error})
    if not row.allow_write:
        return json.dumps({
            'error': f'Connection "{row.name}" does not allow writes. Its owner '
                     'can switch that on.'})
    sql = str(args.get('sql') or '')
    params = args.get('params') or []
    if not isinstance(params, list):
        return json.dumps({'error': '`params` must be a list.'})
    try:
        out = await sync_to_async(drivers.run)(
            row, sql, params, user_id=user_id,
            file_scope=context.get('file_scope'),
            scope_hosts=_egress_hosts(context), write=True)
    except (drivers.DataError, Exception) as exc:
        from data.drivers import DataError

        if isinstance(exc, DataError):
            return json.dumps({'error': str(exc)})
        logger.exception('[Data] execute_sql failed')
        return json.dumps({'error': 'The statement failed.'})
    return json.dumps({**out, 'rendered': (
        f"{out.get('row_count', 0)} row(s) affected on {row.name}.")}, default=str)


DATA_TOOLS = ('list_data_connections', 'describe_schema', 'query_sql', 'execute_sql')
