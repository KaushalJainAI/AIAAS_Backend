"""
Tasks that outlive the request that started them.

Use `spawn()` for any fire-and-forget task whose lifetime is not bounded by the
response — a streamed chat turn, a workflow execution, a periodic flush. Plain
`asyncio.create_task` / `ensure_future` is wrong for those, and fails in a way
that is easy to misread.

Why: Django's ASGI handler runs every request inside
`async with ThreadSensitiveContext()` (`django/core/handlers/asgi.py`), and
whenever a sync middleware has to call back into an async view, asgiref wraps
the frame in an `AsyncToSync` that installs a `CurrentThreadExecutor` for
exactly as long as that frame blocks. Both are held in contextvars, and
`create_task` copies the current context — so a detached task inherits an
executor belonging to a request that is already finished.

Every *thread-sensitive* `sync_to_async` then submits onto that dead executor.
That is what all of Django's async ORM methods are (`aget`, `afirst`, `asave`,
`acreate`, and any `@sync_to_async` helper written without
`thread_sensitive=False`), so the first database call after the response is
sent raises:

    RuntimeError: CurrentThreadExecutor already quit or is broken

The give-away in the log is the traceback landing *after* the matching
`HTTP POST … 200` line: the request completed, the task did not.

`spawn()` starts the task in a fresh `contextvars.Context()` so none of the
request's asgiref state is inherited, and gives it its own
`ThreadSensitiveContext` — a dedicated single-thread executor that lives as
long as the task rather than as long as the response. Django connections opened
on that thread are closed when the task ends, so a long-lived task does not
pin a PostgreSQL connection for the life of the process.

Cancellation, exceptions and the returned `asyncio.Task` behave exactly as with
`create_task`; only the context differs.
"""
from __future__ import annotations

import asyncio
import contextvars
import logging
from typing import Any, Coroutine, TypeVar

from asgiref.sync import ThreadSensitiveContext, sync_to_async
from django.db import close_old_connections

logger = logging.getLogger(__name__)

_T = TypeVar("_T")


async def _detached(coro: Coroutine[Any, Any, _T]) -> _T:
    """Run `coro` under its own thread-sensitive executor, then release the DB."""
    async with ThreadSensitiveContext():
        try:
            return await coro
        finally:
            # Runs on this context's dedicated thread, so it closes the
            # connections that thread opened — not the caller's.
            try:
                await sync_to_async(close_old_connections)()
            except Exception:  # noqa: BLE001 — never mask the task's own outcome
                logger.exception("[Background] Failed to close connections")


def spawn(
    coro: Coroutine[Any, Any, _T],
    *,
    name: str | None = None,
) -> asyncio.Task[_T]:
    """
    Start `coro` as a task detached from the current request's context.

    Requires a running event loop, like `create_task`; it raises the same
    `RuntimeError` if called without one.
    """
    loop = asyncio.get_running_loop()
    return loop.create_task(_detached(coro), name=name, context=contextvars.Context())
