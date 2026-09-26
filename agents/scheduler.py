"""
The trigger sweep, and every other periodic job, running inside the backend
process under a database lease (see "The other periodic jobs" below).

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
import time
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Awaitable, Callable
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
    from agents.sweep import due_triggers

    now = now or timezone.now()
    triggers = await sync_to_async(lambda: list(due_triggers(now)))()
    counts: dict = {}
    for trigger in triggers:
        outcome = await launch(trigger, now)
        counts[outcome] = counts.get(outcome, 0) + 1
    if counts:
        logger.info('[Scheduler] %s',
                    ', '.join(f'{v} {k}' for k, v in sorted(counts.items())))
    return counts


async def launch(trigger, now=None, *, trigger_type: str = 'schedule') -> str:
    """Gate one trigger and start its run detached; return the outcome word.

    The one place a trigger becomes a run without waiting for it -- the
    scheduler loop and the event receivers (e.g. `workspaces.views`) share it,
    so an event cannot skip the gating a schedule gets.
    """
    from agents.agent.runtime import AgentRunRefused, start_agent_run
    from agents.sweep import Launch, prepare, record_failure, record_started

    now = now or timezone.now()
    step = await sync_to_async(prepare)(trigger, now)
    if not isinstance(step, Launch):
        return step
    try:
        execution_id = await start_agent_run(
            trigger.subagent, step.goal, user=trigger.subagent.user,
            trigger_type=trigger_type, caller='trigger',
        )
    except AgentRunRefused as exc:
        await sync_to_async(record_failure)(trigger, now, 'refused', str(exc))
        return 'refused'
    except Exception as exc:  # noqa: BLE001
        logger.exception('[Scheduler] Trigger %s failed to start', trigger.id)
        await sync_to_async(record_failure)(
            trigger, now, 'failed', f'{type(exc).__name__}: {exc}')
        return 'failed'
    await sync_to_async(record_started)(trigger, now, execution_id)
    return 'fired'


# ── The other periodic jobs ─────────────────────────────────────────────────
#
# `CELERY_BEAT_SCHEDULE` lists every periodic job the app has. Production runs
# no Celery, so until 2026-09-26 only this loop's trigger sweep ran there, two
# notification sweeps ran from host cron — each a whole Django process inside
# the backend's 384 MB, which is what kept OOM-killing the web server — and
# run recovery, the recycle-bin purge and checkpoint pruning ran nowhere at
# all. So the loop now runs them too, under the same lease: one process does
# the periodic work, with no cron, no broker and no extra memory.
#
# Each job is detached (`spawn`) so a slow one never delays schedules or lets
# the lease lapse, and a job still running when it next falls due is skipped
# rather than started twice. Intervals are read from settings on each tick,
# the same numbers beat uses. `test_scheduler.py` fails if a beat entry has
# neither a job here nor a reason in `NOT_IN_PROCESS`.


@dataclass(frozen=True)
class PeriodicJob:
    #: The `CELERY_BEAT_SCHEDULE` task this mirrors, so the two lists can be
    #: checked against each other.
    task: str
    #: Settings name holding the interval in seconds.
    every_setting: str
    run: Callable[[], Awaitable[Any]]


async def _scheduled_notifications():
    from notifications.scheduled import run_scheduled_sweep
    return await sync_to_async(run_scheduled_sweep)()


async def _hitl_reminders():
    from notifications.reminders import run_reminder_sweep
    return await sync_to_async(run_reminder_sweep)()


async def _recycle_bin():
    from inference.recycle import run_recycle_sweep
    return await sync_to_async(run_recycle_sweep)()


async def _recover_runs():
    # Runs whose process died (a deploy, an OOM kill) are resumed from their
    # checkpoint or closed; without this they read "running" for ever. `agents`
    # never imports `eval` at module scope, so the eval half is imported here.
    from agents.recovery import sweep_orphaned_runs
    from eval.recovery import sweep_orphaned_eval_runs
    return {**await sweep_orphaned_runs(), 'eval': await sweep_orphaned_eval_runs()}


async def _prune_chat_checkpoints():
    from chat.turn.prune import prune_chat_checkpoints
    return await prune_chat_checkpoints()


PERIODIC_JOBS: tuple[PeriodicJob, ...] = (
    PeriodicJob('notifications.sweep_scheduled', 'SCHEDULED_SWEEP_SECONDS',
                _scheduled_notifications),
    PeriodicJob('notifications.sweep_hitl_reminders', 'HITL_REMINDER_SWEEP_SECONDS',
                _hitl_reminders),
    PeriodicJob('inference.sweep_recycle_bin', 'RECYCLE_SWEEP_SECONDS', _recycle_bin),
    PeriodicJob('orchestrator.recover_runs', 'RUN_RECOVERY_SWEEP_SECONDS', _recover_runs),
    PeriodicJob('orchestrator.prune_chat_checkpoints', 'RUN_RECOVERY_SWEEP_SECONDS',
                _prune_chat_checkpoints),
)

#: Beat entries this loop deliberately does not run, and why.
NOT_IN_PROCESS: dict[str, str] = {
    'orchestrator.sweep_triggers': 'is this loop itself (`sweep_once`)',
    'missions.sweep_missions': (
        'waits for each mission run to finish (`start_agent_run_and_wait`), '
        'which would hold a thread for up to two hours inside the web server; '
        'it needs a detached launch path first'),
    'workspaces.sweep_workspaces': (
        'drives the workspace engine with its own event loop, and no engine '
        'is configured (`WORKSPACE_ENGINE=none`)'),
}

#: When each job last started (monotonic seconds), and its task while running.
_last_started: dict[str, float] = {}
_running: dict[str, asyncio.Task] = {}


async def _run_job(job: PeriodicJob) -> None:
    started = time.monotonic()
    try:
        result = await job.run()
        logger.info('[Scheduler] %s done in %.1fs: %s',
                    job.task, time.monotonic() - started, result)
    except Exception:  # noqa: BLE001 — one failed job must not stop the others
        logger.exception('[Scheduler] %s failed', job.task)


def start_due_jobs(now_monotonic: float | None = None) -> list[str]:
    """Start every periodic job whose interval has passed. Returns their names.

    A job runs on the first tick after the lease is won, then every interval.
    One that is still running is skipped until it finishes.
    """
    now_monotonic = time.monotonic() if now_monotonic is None else now_monotonic
    started: list[str] = []
    for job in PERIODIC_JOBS:
        running = _running.get(job.task)
        if running is not None and not running.done():
            continue
        every = int(getattr(settings, job.every_setting))
        last = _last_started.get(job.task)
        if last is not None and now_monotonic - last < every:
            continue
        _last_started[job.task] = now_monotonic
        _running[job.task] = spawn(_run_job(job), name=f'periodic:{job.task}')
        started.append(job.task)
    return started


async def run_forever() -> None:
    """Sweep on every tick while this process holds the lease. Never exits."""
    while True:
        try:
            now = timezone.now()
            if await sync_to_async(try_acquire)(now):
                await sweep_once(now)
                start_due_jobs()
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
