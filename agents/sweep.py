"""
Firing the triggers that are due.

Deliberately the same shape as `notifications/reminders.py`: one entry point,
reachable both as a Celery beat task and as a management command. That is not
symmetry for its own sake — local development runs without a broker, and a
beat-only design fails by silently never firing, which is the single worst
failure mode a scheduler can have.

What this does *not* do is poll anything. Polling sources with cursors
(email UIDs, RSS GUIDs) are connector-side concerns and a much larger feature
than invocation. A trigger here answers one question: should this agent run now.
"""
from __future__ import annotations

import logging
from datetime import datetime

from asgiref.sync import async_to_sync
from django.utils import timezone

from .triggers import next_run_after

logger = logging.getLogger(__name__)

#: Consecutive failures before a trigger disables itself. A schedule that fails
#: every time is a schedule that will fail every time; leaving it enabled turns
#: one broken agent into an unbounded stream of failed runs and HITL rows.
MAX_CONSECUTIVE_FAILURES = 5

#: How far behind schedule a firing may be and still run. Past this the firing
#: is skipped and the trigger re-armed — catching up on a week of missed 9am
#: runs after an outage is almost never what anyone wants.
MAX_LATENESS_SECONDS = 3600


#: What counts as "still going" — and therefore what `overlap` acts on. A run
#: paused for approval is in flight as much as a running one: it is holding a
#: `HITLRequest` and will resume the moment someone answers.
IN_FLIGHT_STATUSES = ('running', 'paused', 'pending')


def _is_busy(trigger) -> bool:
    """Whether this trigger's agent already has a run in flight."""
    from logs.models import ExecutionLog

    return ExecutionLog.objects.filter(
        subagent_id=trigger.subagent_id, status__in=IN_FLIGHT_STATUSES,
    ).exists()


def _cancel_running(trigger) -> int:
    """Close out the previous run so this firing can replace it.

    Cancels paused runs too. `_is_busy` has always counted `paused`, but this
    did not, so `overlap='cancel'` left an approval-paused run untouched while
    starting a replacement — and the abandoned run kept its pending
    `HITLRequest`, which `notifications/reminders.py` went on escalating and
    putting in the daily digest, asking the user to approve a step belonging to
    a run nothing would ever resume. Withdrawing those rows is the same reason
    `runtime._cancel_pending_hitl` exists on the cancellation path.
    """
    from agents.models import HITLRequest
    from logs.models import ExecutionLog

    doomed = ExecutionLog.objects.filter(
        subagent_id=trigger.subagent_id, status__in=IN_FLIGHT_STATUSES,
    )
    ids = list(doomed.values_list('id', flat=True))
    if not ids:
        return 0

    HITLRequest.objects.filter(execution_id__in=ids, status='pending').update(
        status='cancelled', responded_at=timezone.now(),
    )
    return ExecutionLog.objects.filter(id__in=ids).update(
        status='cancelled', completed_at=timezone.now(),
    )


def _rearm(trigger, now: datetime, *, fired: bool) -> None:
    trigger.next_due_at = next_run_after(trigger.cron, now)
    fields = ['next_due_at', 'updated_at']
    if fired:
        trigger.last_fired_at = now
        fields.append('last_fired_at')
    trigger.save(update_fields=fields)


def fire(trigger, now: datetime | None = None) -> str:
    """
    Run one trigger's agent, and report what happened in a word.

    Returns one of `fired`, `skipped`, `late`, `busy`, `refused`, `failed` —
    the sweep counts these, and a caller reading the counts can tell "nothing
    was due" apart from "everything was refused", which a boolean cannot.
    """
    from agents.agent.runtime import AgentRunRefused, start_agent_run

    now = now or timezone.now()

    if trigger.next_due_at and (now - trigger.next_due_at).total_seconds() > MAX_LATENESS_SECONDS:
        # Missed by more than the grace window — re-arm rather than run a
        # backlog. Firing every missed 9am after an outage is a stampede.
        logger.warning('[Sweep] Trigger %s is too late; skipping to next slot.',
                       trigger.id)
        _rearm(trigger, now, fired=False)
        return 'late'

    if trigger.overlap == 'skip' and _is_busy(trigger):
        _rearm(trigger, now, fired=False)
        return 'busy'
    if trigger.overlap == 'cancel':
        _cancel_running(trigger)

    agent = trigger.subagent
    goal = (trigger.goal or agent.prompt or '').strip()
    if not goal:
        logger.warning('[Sweep] Trigger %s has nothing to ask the agent.', trigger.id)
        _rearm(trigger, now, fired=False)
        return 'skipped'

    try:
        async_to_sync(start_agent_run)(
            agent, goal, user=agent.user, trigger_type='schedule',
            caller='trigger',
        )
    except AgentRunRefused as exc:
        # A guardrail said no — spend cap, or the agent was never cleared for
        # unattended runs. Counted as a failure so a permanently refused
        # trigger eventually disables itself instead of retrying hourly for
        # ever.
        logger.warning('[Sweep] Trigger %s refused: %s', trigger.id, exc)
        _note_failure(trigger, now)
        return 'refused'
    except Exception:  # noqa: BLE001
        logger.exception('[Sweep] Trigger %s failed to start', trigger.id)
        _note_failure(trigger, now)
        return 'failed'

    trigger.consecutive_failures = 0
    trigger.last_fired_at = now
    trigger.next_due_at = next_run_after(trigger.cron, now)
    trigger.save(update_fields=[
        'consecutive_failures', 'last_fired_at', 'next_due_at', 'updated_at',
    ])
    return 'fired'


def _note_failure(trigger, now: datetime) -> None:
    trigger.consecutive_failures += 1
    trigger.next_due_at = next_run_after(trigger.cron, now)
    fields = ['consecutive_failures', 'next_due_at', 'updated_at']

    if trigger.consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
        trigger.enabled = False
        fields.append('enabled')
        logger.error(
            '[Sweep] Trigger %s disabled after %s consecutive failures.',
            trigger.id, trigger.consecutive_failures,
        )

    trigger.save(update_fields=fields)


def run_trigger_sweep(now: datetime | None = None) -> dict[str, int]:
    """
    Fire every schedule trigger that is due. Safe to run more often than the
    schedule: a trigger is re-armed the moment it fires, so a second sweep in
    the same minute finds nothing due.
    """
    from agents.models import Trigger

    now = now or timezone.now()
    counts: dict[str, int] = {}

    due = (
        Trigger.objects
        .filter(enabled=True, mode='schedule', next_due_at__isnull=False,
                next_due_at__lte=now)
        .select_related('subagent', 'subagent__user')
    )

    for trigger in due:
        outcome = fire(trigger, now)
        counts[outcome] = counts.get(outcome, 0) + 1

    if counts:
        logger.info('[Sweep] %s', ', '.join(f'{v} {k}' for k, v in sorted(counts.items())))
    return counts
