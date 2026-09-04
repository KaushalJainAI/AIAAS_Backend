"""
Where a run's state lives, and whether it survives the process.

`MemorySaver` keeps every super-step in a dict. That is fine for a chat turn,
which is over in seconds, and wrong for the thing this platform is trying to
be: an agent run may go forty iterations across two hours, and with an
in-process saver a deploy, a crash or an `ASGI` restart loses all of it with no
way back. The run is a detached task (`background.spawn`), so nothing even
notices — the `ExecutionLog` row simply stays `running` for ever, and the user
watches a spinner attached to nothing.

**One door, selected by setting**, the same shape `sandbox/engine.py` uses and
for the same reason: two ways to store state is two behaviours to reason about
in an incident.

    memory   — in-process, no durability. The default in tests, where a
               file-backed saver would mean writing a database per test.
    sqlite   — a file beside `db.sqlite3`. The dev default: real durability
               with nothing to run.
    postgres — for a deployment where the app has more than one process or a
               container that gets replaced.

Deliberately **not** the Django connection. The checkpointer writes on every
super-step of every run, from detached tasks that outlive their request, and
sharing Django's pool would put that traffic behind the same connections
serving HTTP — where `CONN_MAX_AGE`, PgBouncer's transaction pooling and
Django's own `close_old_connections` all apply to a writer that has none of a
request's lifecycle. Its own pool is the boring choice.

There is no automatic fallback between backends. A configured `postgres` saver
that cannot connect raises at startup rather than quietly degrading to
`memory`, because "durable" that silently is not is worse than never having
claimed it: the resume sweep would find rows to resume and no state to resume
them from.
"""
from __future__ import annotations

import logging
from pathlib import Path

from django.conf import settings

logger = logging.getLogger(__name__)

#: What was actually built, for the health endpoint and for tests that need to
#: skip when the backend is not durable.
active_backend: str = 'memory'


def _configured() -> str:
    """The backend this deployment asked for, normalised."""
    raw = getattr(settings, 'AGENT_CHECKPOINTER', '') or ''
    choice = str(raw).strip().lower()
    if choice in ('memory', 'sqlite', 'postgres'):
        return choice
    if choice:
        logger.warning('[Checkpoints] Unknown AGENT_CHECKPOINTER %r; using memory', raw)
    return 'memory'


def build():
    """The checkpointer this process will use. Called once, at graph build.

    Returns a saver whose `aput`/`aget_tuple` the graph drives. Every backend
    here is opened *without* a context manager on purpose: the saver has to
    outlive the function that made it and live as long as the process, which is
    exactly the lifetime of the compiled graph it is handed to.
    """
    global active_backend

    choice = _configured()
    if choice == 'memory':
        active_backend = 'memory'
        return _memory()

    if choice == 'sqlite':
        try:
            saver = _sqlite()
        except Exception:
            # Dev-only backend, and the failure is almost always a missing
            # optional package. Degrading here is safe *because* the resume
            # sweep asks `is_durable()` rather than assuming: it will report
            # that it cannot resume instead of finding rows with no state.
            logger.exception(
                '[Checkpoints] SQLite checkpointer unavailable; '
                'runs will not survive a restart'
            )
            active_backend = 'memory'
            return _memory()
        active_backend = 'sqlite'
        return saver

    # Postgres is the production choice, so a failure here is *not* swallowed.
    saver = _postgres()
    active_backend = 'postgres'
    return saver


def _memory():
    from langgraph.checkpoint.memory import MemorySaver

    return MemorySaver()


def _sqlite():
    """A file-backed saver beside the dev database.

    Its own file rather than `db.sqlite3`: checkpoint traffic is write-heavy
    and would take SQLite's single write lock on the application database on
    every super-step of every run, which is how a background agent starts
    blocking page loads.
    """
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
    import aiosqlite

    path = Path(
        getattr(settings, 'AGENT_CHECKPOINT_PATH', '')
        or Path(settings.BASE_DIR) / 'checkpoints.sqlite3'
    )
    path.parent.mkdir(parents=True, exist_ok=True)

    conn = aiosqlite.connect(str(path), check_same_thread=False)
    saver = AsyncSqliteSaver(conn)
    logger.info('[Checkpoints] SQLite checkpointer at %s', path)
    return saver


def _postgres():
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
    from psycopg_pool import AsyncConnectionPool

    dsn = getattr(settings, 'AGENT_CHECKPOINT_DSN', '') or getattr(
        settings, 'DATABASE_URL', ''
    )
    if not dsn:
        raise RuntimeError(
            'AGENT_CHECKPOINTER=postgres needs AGENT_CHECKPOINT_DSN (or '
            'DATABASE_URL). Refusing to start rather than silently losing runs.'
        )

    # `open=False`: opening a pool needs a running loop, and this is called at
    # import time while the graph is compiled. The saver opens it on first use.
    pool = AsyncConnectionPool(conninfo=dsn, max_size=10, open=False,
                               kwargs={'autocommit': True, 'prepare_threshold': 0})
    saver = AsyncPostgresSaver(pool)
    logger.info('[Checkpoints] Postgres checkpointer configured')
    return saver


def is_durable() -> bool:
    """Whether a run's state would survive this process going away.

    Asked rather than assumed by anything that promises persistence — the
    resume sweep above all, which must say "I cannot resume these" rather than
    look for state that was never written.
    """
    return active_backend != 'memory'


async def setup(saver) -> None:
    """Create the backend's tables, if it has any. Idempotent.

    Both file-backed savers need a one-time schema. `AsyncSqliteSaver` calls
    this itself before its first write — and, importantly, it is also what
    starts the `aiosqlite` connection, which `build()` cannot do because
    connecting is a coroutine and the graph is compiled from sync code. So the
    ordering is: construct unconnected here, connect on first real use.

    Called explicitly by the recovery sweep and the management command, both of
    which *read* state before anything has written any — without it they would
    query a database with no tables and conclude, wrongly, that no run has
    state worth resuming.
    """
    setup_fn = getattr(saver, 'setup', None)
    if setup_fn is None:
        return
    try:
        result = setup_fn()
        if hasattr(result, '__await__'):
            await result
    except Exception:  # noqa: BLE001
        logger.warning('[Checkpoints] Backend setup failed', exc_info=True)
