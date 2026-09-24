"""
How often the server gets stuck, and how full the database pool is.

Phase 0 of `docs/CONCURRENCY_LAG_FIX_PLAN.md`: code-reading named the suspects
(a blocked event loop, an exhausted DB pool) but measured neither, so this
module turns both into numbers before anything is changed — and gives every
later phase a before/after.

Two instruments, same contract as `[Latency] pre-model` and
`agent._log_latency`: one line, grep-able, cheap to emit.

- **Loop lag**: sleep 1 s; log `[Latency] loop-lag <ms>` when the wake-up is
  more than 100 ms late. Anything doing CPU work on the loop (checkpoint
  serialisation) or blocking it delays this sleep, so the line is the direct
  answer to "is something blocking the loop" (G2, G3).
- **Pool pressure**: about once a minute, log the psycopg pool counters
  (`pool_size`, `pool_available`, `requests_waiting`, ...). Silent when the
  pool is off (SQLite dev) — there is nothing to be full of (G1).

Started next to the scheduler (`agents.scheduler.ensure_started`), one task
per process. It never touches the ORM — pool counters are read, not queried —
so it holds no connection and needs none.
"""
from __future__ import annotations

import asyncio
import logging
import time

from workflow_backend.background import spawn

logger = logging.getLogger(__name__)

#: How often the watchdog wakes. A delay here *is* the measurement.
TICK_SECONDS = 1.0

#: A wake-up this late means the loop was busy doing something else.
LAG_WARN_MS = 100

#: Pool counters are slow-moving; waking every tick to read them is noise.
POOL_LOG_EVERY_TICKS = 60


def _pool_stats() -> dict | None:
    """The psycopg pool counters, or `None` when there is no pool to read.

    Never raises and never queries: `connection.pool` is `None` when the pool
    is off (SQLite dev), and `get_stats()` reads the pool's own counters.
    Anything unexpected — a pool that was never opened, a version with a
    different shape — means "no data", not an error in the thing measuring.
    """
    try:
        from django.db import connection

        pool = connection.pool
    except Exception:  # noqa: BLE001 — the watcher must never die on this
        return None
    if pool is None:
        return None
    try:
        stats = pool.get_stats()
    except Exception:  # noqa: BLE001 — see above
        return None
    if not isinstance(stats, dict):
        return None
    return stats


async def _watch_forever() -> None:
    """Sleep 1 s; report how late the wake-up was, plus pool counters."""
    last = time.monotonic()
    ticks = 0
    while True:
        try:
            await asyncio.sleep(TICK_SECONDS)
            now = time.monotonic()
            lag_ms = (now - last - TICK_SECONDS) * 1000
            last = now
            if lag_ms >= LAG_WARN_MS:
                logger.info("[Latency] loop-lag %dms", int(lag_ms))
            ticks += 1
            if ticks % POOL_LOG_EVERY_TICKS == 0:
                stats = _pool_stats()
                if stats:
                    logger.info(
                        "[DB] pool size=%s available=%s waiting=%s wait_ms=%s errors=%s",
                        stats.get("pool_size", "?"),
                        stats.get("pool_available", "?"),
                        stats.get("requests_waiting", "?"),
                        stats.get("requests_wait_ms", "?"),
                        stats.get("requests_errors", "?"),
                    )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — a dead watcher measures nothing
            logger.exception("[Loopwatch] tick failed")
            last = time.monotonic()


_started = False


def ensure_started() -> None:
    """Start the watchdog once per process. Called next to the scheduler."""
    global _started
    if _started:
        return
    _started = True
    spawn(_watch_forever(), name="loopwatch")
