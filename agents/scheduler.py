"""
The trigger sweep, running inside the backend process under a database lease.

Production used to depend on a host crontab someone had to add by hand, and
local dev had nothing at all — so schedules silently never fired. The loop
below lives in the backend process itself (Daphne in prod, `runserver`
locally), costs no extra memory or broker, and starts on the first HTTP
request.

Two guards make it safe to run alongside anything else that still fires the
sweep (Celery beat, `manage.py run_due_triggers`, a leftover crontab):

- the **lease** (`SchedulerLease`): only the process holding it sweeps, and a
  dead holder stops owning it after `LEASE_SECONDS`;
- the **per-slot claim** (`sweep.prepare`): one slot can never fire twice,
  even if two sweeps read it due in the same second.

Both are single conditional UPDATEs — no advisory locks, no
`select_for_update` — so they are atomic on SQLite and Postgres alike.
"""
from __future__ import annotations

import asyncio
import logging
import os
import socket
from datetime import timedelta
from uuid import uuid4

from asgiref.sync import sync_to_async
from django.conf import settings
from django.db import IntegrityError
from django.db.models import Q
from django.utils import timezone

from workflow_backend.background import release_db, spawn

logger = logging.getLogger(__name__)

#: Which `SchedulerLease` row this loop holds.
LEASE_NAME = 'triggers'

#: Cron resolution is one minute; ticking twice a minute means a firing is
#: never late by more than half a minute for scheduler reasons.
TICK_SECONDS = 30

#: Three missed ticks before another process may take the lease.
LEASE_SECONDS = 90

#: Who holds the lease. Host + pid + random fragment: two processes on one
#: box differ by pid, two boxes differ by host, and a restarted process with
#: a recycled pid differs by fragment.
HOLDER = f'{socket.gethostname()}:{os.getpid()}:{uuid4().hex[:6]}'


def try_acquire(now=None) -> bool:
    """Take or renew the lease. One conditional UPDATE, so it is atomic."""
    from agents.models import SchedulerLease

    now = now or timezone.now()
    try:
        SchedulerLease.objects.get_or_create(
            name=LEASE_NAME,
            # A brand-new lease is unowned: backdate both columns so the
            # first UPDATE wins it and the health check reads "not running"
            # until a tick actually lands — a row created a second ago that
            # nobody holds must not report a live scheduler.
            defaults={'holder': '',
                      'expires_at': now - timedelta(seconds=1),
                      'beat_at': now - timedelta(seconds=2 * LEASE_SECONDS + 1)},
        )
    except IntegrityError:
        # Another process created the row between our read and our write.
        # Either way it exists now, which is all the UPDATE below needs.
        pass
    return SchedulerLease.objects.filter(name=LEASE_NAME).filter(
        Q(holder=HOLDER) | Q(expires_at__lt=now)
    ).update(
        holder=HOLDER,
        expires_at=now + timedelta(seconds=LEASE_SECONDS),
        beat_at=now,
    ) == 1


def lease_status(now=None) -> dict:
    """What the health endpoint reads: is anyone sweeping, and since when."""
    from agents.models import SchedulerLease

    now = now or timezone.now()
    try:
        lease = SchedulerLease.objects.get(name=LEASE_NAME)
    except SchedulerLease.DoesNotExist:
        return {'running': False, 'last_tick_at': None}
    return {
        'running': lease.beat_at > now - timedelta(seconds=2 * LEASE_SECONDS),
        'last_tick_at': lease.beat_at.isoformat() if lease.beat_at else None,
    }


async def sweep_once(now=None) -> dict:
    """Fire every due schedule, without waiting for any run.

    The sync `run_trigger_sweep` (beat, management command) blocks on each
    run to completion; this one starts each run detached, so one slow agent
    no longer holds up every other schedule. Gating, re-arming and failure
    counting are the same `prepare` / `record_*` the sync path uses — the two
    callers differ in waiting, never in rules.
    """
    from agents.agent.runtime import AgentRunRefused, start_agent_run
    from agents.sweep import (
        Launch,
        due_triggers,
        prepare,
        record_failure,
        record_started,
    )

    now = now or timezone.now()
    triggers = await sync_to_async(lambda: list(due_triggers(now)))()
    counts: dict = {}
    for trigger in triggers:
        step = await sync_to_async(prepare)(trigger, now)
        if not isinstance(step, Launch):
            counts[step] = counts.get(step, 0) + 1
            continue
        try:
            execution_id = await start_agent_run(
                trigger.subagent, step.goal, user=trigger.subagent.user,
                trigger_type='schedule', caller='trigger',
            )
        except AgentRunRefused as exc:
            await sync_to_async(record_failure)(trigger, now, 'refused', str(exc))
            counts['refused'] = counts.get('refused', 0) + 1
            continue
        except Exception as exc:  # noqa: BLE001
            logger.exception('[Scheduler] Trigger %s failed to start', trigger.id)
            await sync_to_async(record_failure)(
                trigger, now, 'failed', f'{type(exc).__name__}: {exc}')
            counts['failed'] = counts.get('failed', 0) + 1
            continue
        await sync_to_async(record_started)(trigger, now, execution_id)
        counts['fired'] = counts.get('fired', 0) + 1
    if counts:
        logger.info('[Scheduler] %s',
                    ', '.join(f'{v} {k}' for k, v in sorted(counts.items())))
    return counts


async def run_forever() -> None:
    """Sweep on every tick while this process holds the lease. Never exits."""
    while True:
        try:
            now = timezone.now()
            if await sync_to_async(try_acquire)(now):
                await sweep_once(now)
        except Exception:  # noqa: BLE001 — a dead scheduler fires nothing
            logger.exception('[Scheduler] tick failed')
        # The 30 s tick is a sleep, not database work. The sweep borrows a
        # connection per tick; without this the scheduler pins one for ever.
        await release_db()
        await asyncio.sleep(TICK_SECONDS)


_started = False


def ensure_started() -> None:
    """Start the loop once per process. Called on the first HTTP request."""
    global _started
    if _started or not settings.SCHEDULER_ENABLED:
        return
    _started = True
    spawn(run_forever(), name='scheduler')
    # Phase 0 instrument (CONCURRENCY_LAG_FIX_PLAN.md): the loop-lag watchdog
    # lives next to the scheduler because both are one-task-per-process loops
    # started from the request path. It holds no DB connection, so it starts
    # unconditionally once the scheduler does.
    from workflow_backend.loopwatch import ensure_started as ensure_loopwatch
    ensure_loopwatch()
