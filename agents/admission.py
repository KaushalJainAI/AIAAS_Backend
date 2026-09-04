"""Who gets to run right now, so that one user is not every user.

The spend cap bounds what an agent costs its *owner* over a month. Nothing
bounded what it costs everyone else over the next thirty seconds. Twenty
schedules firing at 09:00, or one fan-out of eight repeated by a trigger, is a
box with no capacity left for anybody — and on a single-instance deployment
"the box" and "the product" are the same thing.

So a top-level run takes a slot before it starts, and waits if there is none.

**Two gates, not one.** The per-user gate is the fairness one: it stops a
single account monopolising the box. The global gate is the survival one: eight
users at their per-user limit is still more concurrent runs than a small
instance can hold. Neither implies the other, so both exist.

**Workers are not gated, deliberately.** A delegated run is started *by* a run
that already holds a slot and is awaiting it. Gating the worker on the same
semaphore is not a limit, it is a deadlock — the parent can only release when
the worker finishes, and the worker can only start when the parent releases.
Delegation is bounded instead by depth, by `MAX_PARALLEL_WORKERS`, by the
divided spend budget and by the shared deadline (`agents/budget.py`), which are
bounds that compose rather than wait on each other.

**Per-process.** These semaphores live in this process's event loop, so they
bound one ASGI worker. That is the honest scope, and it is the deployment's
shape (one box, one process). At N processes the effective limit is N times
these numbers; the fix then is a shared counter in Redis, not a smaller number
here that would still be wrong.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import AsyncIterator

from workflow_backend.thresholds import (
    ADMISSION_WAIT_SECONDS,
    MAX_CONCURRENT_RUNS_PER_USER,
    MAX_CONCURRENT_RUNS_TOTAL,
)

logger = logging.getLogger(__name__)


class AdmissionTimeout(Exception):
    """The run waited for a slot and never got one."""


#: Keyed by user id. Created on demand and never evicted: one `Semaphore` per
#: user who has ever run an agent in this process is a few hundred bytes, and
#: evicting one while a run holds it would hand the next caller a fresh
#: semaphore with the full count — a limit that resets under load is not one.
_per_user: dict[int, asyncio.Semaphore] = {}
_global: asyncio.Semaphore | None = None


def _user_gate(user_id: int) -> asyncio.Semaphore:
    gate = _per_user.get(user_id)
    if gate is None:
        gate = asyncio.Semaphore(MAX_CONCURRENT_RUNS_PER_USER)
        _per_user[user_id] = gate
    return gate


def _global_gate() -> asyncio.Semaphore:
    # Built lazily rather than at import: `asyncio.Semaphore` binds to the
    # running loop on first await, and this module is imported at startup when
    # there may not be one — under the test runner there are several.
    global _global
    if _global is None:
        _global = asyncio.Semaphore(MAX_CONCURRENT_RUNS_TOTAL)
    return _global


@contextlib.asynccontextmanager
async def slot(user_id: int, *, wait: float = ADMISSION_WAIT_SECONDS) -> AsyncIterator[None]:
    """Hold a run slot for the body, or raise `AdmissionTimeout`.

    The global gate is taken *first* and the per-user gate second, always in
    that order. Two locks acquired in opposite orders by different callers is
    the textbook deadlock; one order, stated here, is the whole prevention.

    The wait is deliberately outside the run's own time budget — `run_agent`
    starts its clock after this returns. A run that queued for ninety seconds
    has not spent ninety seconds of the limit its owner set for the work.
    """
    outer = _global_gate()
    inner = _user_gate(user_id)
    try:
        async with asyncio.timeout(wait):
            await outer.acquire()
            try:
                await inner.acquire()
            except BaseException:
                outer.release()
                raise
    except TimeoutError as exc:
        raise AdmissionTimeout(
            'The system is at capacity and this run could not be started '
            f'within {int(wait)}s. It has not been charged for; try again '
            'shortly, or stagger the schedules that fire together.'
        ) from exc

    try:
        yield
    finally:
        inner.release()
        outer.release()


def snapshot() -> dict:
    """What the gates look like right now. For diagnostics, never for control."""
    return {
        'global_free': _global_gate()._value,  # noqa: SLF001 — read-only probe
        'global_limit': MAX_CONCURRENT_RUNS_TOTAL,
        'per_user_limit': MAX_CONCURRENT_RUNS_PER_USER,
        'users_tracked': len(_per_user),
    }


def _reset_for_tests() -> None:
    """Drop every gate. Tests get a fresh event loop each; semaphores do not."""
    global _global
    _per_user.clear()
    _global = None
