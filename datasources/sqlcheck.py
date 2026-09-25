"""
What SQL a run may say, decided by parsing rather than by pattern-matching.

One statement only (`sqlparse` splits; more than one is refused), and the
first keyword decides: reads are SELECT / WITH / VALUES / EXPLAIN / TABLE.
Everything else — including `;`-chained writes and `COPY` — is refused with
the reason, because a trimmed instruction is a worker confidently doing the
wrong job and the author here is a model that can be told to rephrase.
"""
from __future__ import annotations

import sqlparse

#: First keywords that only read.
READ_FIRST = frozenset({'SELECT', 'WITH', 'VALUES', 'EXPLAIN', 'TABLE'})

class SqlRefused(ValueError):
    """The statement is not a read (or not one statement)."""


def check(sql: str, *, write: bool = False) -> str:
    """Validate `sql` for a read, or for a write when `write` is set.

    Writes still go through here: one statement only, and the same refused
    words (a write tool is how a model reaches DDL, and "it was approved"
    covers the statement on the card, not a smuggled second one).
    Returns the stripped statement.
    """
    text = (sql or '').strip().rstrip(';').strip()
    if not text:
        raise SqlRefused('Give the SQL to run.')
    try:
        statements = [s for s in sqlparse.split(text) if s.strip()]
    except Exception as exc:
        raise SqlRefused(f'That SQL could not be parsed: {exc}.') from exc
    if len(statements) != 1:
        raise SqlRefused('One statement per call. Split it into separate calls.')
    statement = statements[0]
    try:
        flat = [t for t in sqlparse.parse(statement)[0].flatten()
                if not t.is_whitespace and t.ttype not in sqlparse.tokens.Comment]
    except (IndexError, Exception) as exc:
        raise SqlRefused(f'That SQL could not be parsed: {exc}.') from exc
    # Token types form a hierarchy (`Token.Keyword.DML`); match by prefix
    # rather than enumerating every subtype.
    keyed = [str(t).upper() for t in flat
             if str(t.ttype).startswith('Token.Keyword')]
    named = [str(t).upper() for t in flat
             if str(t.ttype).startswith('Token.Name')]
    if not keyed:
        raise SqlRefused('That is not a SQL statement this tool runs.')
    # sqlparse tokenises PRAGMA as a *name*, so the keyword scan below misses
    # it; the leading word is checked on its own (N7).
    if flat and str(flat[0]).upper() in ('PRAGMA', 'ATTACH', 'DETACH'):
        raise SqlRefused(f'`{str(flat[0]).upper()}` reaches outside the query.')
    if not write and keyed[0] not in READ_FIRST:
        raise SqlRefused(
            f'`{keyed[0]}` does not read. query_sql runs SELECT / WITH / '
            'VALUES / EXPLAIN only; writes go through execute_sql on a '
            'connection whose owner allowed them.')
    # Schema and server work is never data work, approved or not: the card
    # approves the statement shown, not a smuggled second act.
    hit = next((w for w in keyed if w in (
        'DROP', 'CREATE', 'ALTER', 'TRUNCATE', 'GRANT', 'REVOKE', 'COPY',
        'VACUUM', 'CALL', 'DO', 'LISTEN', 'NOTIFY', 'SECURITY',
        # SQLite: ATTACH opens (or creates) any file the server can reach, and
        # PRAGMA can switch `query_only` back off (N7).
        'ATTACH', 'DETACH', 'PRAGMA')), None)
    if hit:
        raise SqlRefused(f'`{hit}` is schema or server work, not data work.')
    # Function calls are names, not keywords — but a name cannot smuggle
    # server file reads past the keyword scan.
    hit = next((w for w in named if w in ('PG_READ_FILE', 'PG_SLEEP')), None)
    if hit:
        raise SqlRefused(f'`{hit}` reaches outside the database.')
    if not write:
        hit = next((w for w in keyed if w in (
            'INSERT', 'UPDATE', 'DELETE', 'MERGE', 'REPLACE', 'INTO')), None)
        if hit:
            raise SqlRefused(
                f'`{hit}` writes. query_sql reads; writes go through '
                'execute_sql on a connection whose owner allowed them.')
    return statement
