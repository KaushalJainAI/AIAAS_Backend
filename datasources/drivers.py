"""
One door to the user's databases, per connection kind.

Postgres and MySQL open short-lived connections with a 30 s statement
timeout and run reads inside a read-only transaction. SQLite opens the
user's own database file, resolved through their `FileScope` — the only
kind with no network and no credentials. BigQuery and MySQL need drivers
this image does not carry (`pymysql`, `google-cloud-bigquery` are optional
extras); a missing driver means the kind is not offered, never half-served.
"""
from __future__ import annotations

import logging
import sqlite3

logger = logging.getLogger(__name__)

#: Rows one `query_sql` call returns inline. Past it the result is written to
#: a CSV in the caller's write folder and the path comes back instead — a
#: capped table and a complete one must not look alike.
ROW_CAP = 1_000
#: Rows one spilled CSV may hold. Past it the call is refused: fifty thousand
#: rows is already a dataset, not an answer, and the model should narrow the
#: query rather than page through it all.
MAX_SPILL_ROWS = 50_000
STATEMENT_TIMEOUT_MS = 30_000


class DataError(ValueError):
    """Written for the model: what failed and what to do instead."""


def available_kinds() -> tuple[str, ...]:
    """Kinds this process can actually drive."""
    kinds = ['postgres', 'sqlite']
    try:
        import pymysql  # noqa: F401

        kinds.append('mysql')
    except ImportError:
        pass
    try:
        import google.cloud.bigquery  # noqa: F401

        kinds.append('bigquery')
    except ImportError:
        pass
    return tuple(kinds)


def _resolve_password(secret_ref: str, user_id: int) -> str:
    """A password out of the vault, or '' when there is none to resolve."""
    if not secret_ref:
        return ''
    import re as _re

    from asgiref.sync import async_to_sync

    from credentials.manager import CredentialManager

    manager = CredentialManager()

    async def _get() -> str | None:
        match = _re.fullmatch(r'\s*([A-Za-z0-9][A-Za-z0-9_-]*)\.([A-Za-z0-9_]+)\s*',
                              secret_ref)
        if match:
            data = await manager.get_credential_by_slug(
                match.group(1), user_id)
            if data:
                return data.get(match.group(2))
            return None
        credential = await manager.get_credential(secret_ref, user_id)
        if not credential:
            return None
        return credential.get('password') or credential.get('api_key')

    try:
        return async_to_sync(_get)() or ''
    except Exception:  # noqa: BLE001
        logger.exception('[Data] Could not resolve database credential')
        return ''


def _check_host(connection, scope_hosts: list) -> None:
    """The P0 egress guard, or raise `DataError`.

    The connection's own host is allowed by virtue of being configured;
    `dbHosts` adds to it. SSRF always applies — except when
    `DATA_ALLOW_PRIVATE_HOSTS` is set for self-hosted deployments, in which
    case the owner's own configured host is trusted as written.
    """
    from django.conf import settings as _settings

    from core.safety.net import check_egress

    if connection.kind == 'sqlite':
        return
    host = connection.host or ''
    if not host:
        raise DataError('That connection names no host.')
    if getattr(_settings, 'DATA_ALLOW_PRIVATE_HOSTS', False):
        return
    allowed = {host}
    allowed.update(str(h) for h in scope_hosts if str(h).strip())
    ok, reason = check_egress(host, {'dbHosts': sorted(allowed)})
    if not ok:
        raise DataError(reason)


def _sqlite_path(connection, file_scope):
    if not connection.vfs_path:
        raise DataError('That SQLite connection names no file. Set its file first.')
    if file_scope is None:
        raise DataError('There is no file workspace here to open that database from.')
    from inference import vfs

    parent_parts, leaf = vfs._split_leaf(file_scope, connection.vfs_path)
    folder = vfs._folder_at(file_scope, parent_parts)
    doc = vfs._document_in(file_scope, folder, leaf)
    if doc is None or not doc.file:
        raise DataError(
            f'No database file at {connection.vfs_path}. Upload it first.')
    return doc.file.path


def run(connection, sql: str, params: list | None, *, user_id: int,
        file_scope=None, scope_hosts: list | None = None,
        write: bool = False, full: bool = False,
        row_cap: int = ROW_CAP) -> dict:
    """Run one validated statement. Returns {columns, rows, row_count, more}.

    Inline calls fetch `row_cap + 1` rows and set `more` when there are more
    than fit; `full=True` fetches up to `MAX_SPILL_ROWS` for the CSV spill
    and refuses past it. `row_cap` is the caller's workspace knob
    (`query_sql.maxRows`) resolved by the tool layer; the module constant
    stays the default for callers without one.
    """
    from . import sqlcheck

    sqlcheck.check(sql, write=write)
    _check_host(connection, list(scope_hosts or []))
    if connection.kind not in available_kinds():
        raise DataError(
            f'{connection.kind} needs a driver this platform does not carry.')
    if connection.kind == 'sqlite':
        return _run_sqlite(connection, sql, params or [], file_scope,
                           write=write, full=full, row_cap=row_cap)
    if connection.kind == 'postgres':
        return _run_postgres(connection, sql, params or [], user_id,
                             write=write, full=full, row_cap=row_cap)
    if connection.kind == 'mysql':
        return _run_mysql(connection, sql, params or [], user_id,
                          write=write, full=full, row_cap=row_cap)
    raise DataError(f'{connection.kind} is not wired yet.')


def _fetch(cursor, *, full: bool, row_cap: int = ROW_CAP) -> tuple[list[str], list[list], bool]:
    """Columns, rows and whether rows were left behind."""
    columns = [d[0] for d in (cursor.description or [])]
    if full:
        rows = [list(r) for r in cursor.fetchmany(MAX_SPILL_ROWS + 1)]
        if len(rows) > MAX_SPILL_ROWS:
            raise DataError(
                f'More than {MAX_SPILL_ROWS:,} rows match. Narrow the query '
                'with a WHERE clause or an aggregation instead of paging '
                'through it all.')
        return columns, rows, False
    rows = [list(r) for r in cursor.fetchmany(row_cap + 1)]
    if len(rows) > row_cap:
        return columns, rows[:row_cap], True
    return columns, rows, False


def _run_sqlite(connection, sql: str, params: list, file_scope, *,
                write: bool, full: bool = False, row_cap: int = ROW_CAP) -> dict:
    path = _sqlite_path(connection, file_scope)
    uri = f'file:{path}?mode={"rw" if write else "ro"}'
    try:
        db = sqlite3.connect(uri, uri=True, timeout=STATEMENT_TIMEOUT_MS / 1000)
    except sqlite3.Error as exc:
        raise DataError(f'The database file could not be opened: {exc}.') from exc
    try:
        db.execute('PRAGMA query_only=ON') if not write else None
        cursor = db.execute(sql, params)
        if cursor.description is None:
            db.commit() if write else None
            return {'columns': [], 'rows': [], 'row_count': cursor.rowcount, 'more': False}
        columns, rows, more = _fetch(cursor, full=full, row_cap=row_cap)
        return {'columns': columns, 'rows': rows, 'row_count': len(rows), 'more': more}
    except sqlite3.Error as exc:
        raise DataError(f'The query failed: {exc}.') from exc
    finally:
        db.close()


def _run_postgres(connection, sql: str, params: list, user_id: int, *,
                  write: bool, full: bool = False, row_cap: int = ROW_CAP) -> dict:
    import psycopg

    password = _resolve_password(connection.secret_ref, user_id)
    try:
        with psycopg.connect(
            host=connection.host, port=connection.port or 5432,
            dbname=connection.database, user=connection.username or None,
            password=password or None,
            connect_timeout=10,
            options=f'-c statement_timeout={STATEMENT_TIMEOUT_MS}',
        ) as db:
            if not write:
                db.execute('START TRANSACTION READ ONLY')
            try:
                with db.cursor() as cursor:
                    cursor.execute(sql, params)
                    if cursor.description is None:
                        db.commit()
                        return {'columns': [], 'rows': [],
                                'row_count': cursor.rowcount, 'more': False}
                    columns, rows, more = _fetch(cursor, full=full, row_cap=row_cap)
                    db.commit() if write else db.rollback()
                    return {'columns': columns, 'rows': rows,
                            'row_count': len(rows), 'more': more}
            finally:
                try:
                    db.rollback()
                except Exception:  # noqa: BLE001
                    pass
    except Exception as exc:  # noqa: BLE001
        if isinstance(exc, DataError):
            raise
        raise DataError(f'The query failed: {exc}.') from exc


def _run_mysql(connection, sql: str, params: list, user_id: int, *,
               write: bool, full: bool = False, row_cap: int = ROW_CAP) -> dict:
    import pymysql

    password = _resolve_password(connection.secret_ref, user_id)
    try:
        db = pymysql.connect(
            host=connection.host, port=connection.port or 3306,
            database=connection.database or None,
            user=connection.username or None, password=password or None,
            connect_timeout=10, read_timeout=STATEMENT_TIMEOUT_MS // 1000,
            write_timeout=STATEMENT_TIMEOUT_MS // 1000,
        )
    except Exception as exc:  # noqa: BLE001
        raise DataError(f'The database could not be reached: {exc}.') from exc
    try:
        with db.cursor() as cursor:
            if not write:
                cursor.execute('START TRANSACTION READ ONLY')
            cursor.execute(sql, params)
            if cursor.description is None:
                db.commit()
                return {'columns': [], 'rows': [], 'row_count': cursor.rowcount,
                        'more': False}
            columns, rows, more = _fetch(cursor, full=full, row_cap=row_cap)
            db.commit() if write else db.rollback()
            return {'columns': columns, 'rows': rows, 'row_count': len(rows),
                    'more': more}
    except Exception as exc:  # noqa: BLE001
        if isinstance(exc, DataError):
            raise
        raise DataError(f'The query failed: {exc}.') from exc
    finally:
        try:
            db.close()
        except Exception:  # noqa: BLE001
            pass


def describe(connection, *, table: str = '', user_id: int = 0,
             file_scope=None, scope_hosts: list | None = None) -> dict:
    """Tables, or one table's columns."""
    _check_host(connection, list(scope_hosts or []))
    if connection.kind == 'sqlite':
        path = _sqlite_path(connection, file_scope)
        db = sqlite3.connect(f'file:{path}?mode=ro', uri=True, timeout=10)
        try:
            tables = [r[0] for r in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
            if not table:
                return {'tables': tables}
            if table not in tables:
                raise DataError(f'No table {table!r}. Tables: {", ".join(tables) or "none"}.')
            cols = db.execute(f'PRAGMA table_info("{table}")').fetchall()
            return {'table': table,
                    'columns': [{'name': c[1], 'type': c[2]} for c in cols]}
        finally:
            db.close()
    if connection.kind == 'postgres':
        if table:
            out = run(connection,
                      'SELECT column_name, data_type FROM information_schema.columns '
                      'WHERE table_name = %s ORDER BY ordinal_position',
                      [table], user_id=user_id, scope_hosts=scope_hosts)
            if not out['rows']:
                raise DataError(f'No table {table!r} is visible.')
            return {'table': table,
                    'columns': [{'name': r[0], 'type': r[1]} for r in out['rows']]}
        out = run(connection,
                  "SELECT tablename FROM pg_tables WHERE schemaname NOT IN "
                  "('pg_catalog', 'information_schema') ORDER BY tablename",
                  [], user_id=user_id, scope_hosts=scope_hosts)
        return {'tables': [r[0] for r in out['rows']]}
    raise DataError(f'{connection.kind} schema listing is not wired yet.')
