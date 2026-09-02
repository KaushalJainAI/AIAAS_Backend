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

#: How long a firing deferred by `overlap='queue'` stays owed. Past this it is
#: dropped: a queued 09:00 report delivered at midnight is not the report
#: anyone asked for, and a queue with no expiry turns one stuck run into a
#: burst of every firing it blocked.
QUEUE_TTL_SECONDS = 6 * 3600


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


def _rearm(trigger, now: datetime, outcome: str = '', error: str = '', *,
           fired: bool = False) -> str:
    """Point the trigger at its next slot and record how this one went.

    Returns the outcome it was given, so every branch in `fire` can end in
    `return _rearm(...)` and none of them can forget to write the row back.

    A schedule the cron walker cannot satisfy — `0 0 30 2 *`, or a window that
    has closed — is **disabled with a reason** rather than left with a NULL
    `next_due_at`. A row that is enabled, has no next run, and says nothing
    about why looks exactly like a working one in every listing.
    """
    # Arm from the start date, not from now, when the window has not opened:
    # otherwise a schedule starting next month is re-armed to tomorrow, the
    # sweep picks it up every day until then, and each pass writes a row it
    # only has to write back again.
    base = (trigger.starts_at
            if trigger.starts_at and trigger.starts_at > now else now)
    trigger.next_due_at = next_run_after(trigger.cron, base, trigger.tz)
    trigger.last_outcome = outcome
    trigger.last_error = error
    fields = ['next_due_at', 'last_outcome', 'last_error', 'updated_at']

    if trigger.next_due_at is None and trigger.enabled:
        trigger.enabled = False
        trigger.last_outcome = 'stopped'
        trigger.last_error = (
            f'This schedule has no next run: "{trigger.cron}" never comes '
            f'round again in {trigger.tz}.'
        )
        fields.append('enabled')
        outcome = 'stopped'
    elif trigger.ends_at and trigger.next_due_at and trigger.next_due_at > trigger.ends_at:
        # The next slot falls outside the window, so there will never be
        # another. Closing it now beats leaving a row that looks armed.
        trigger.enabled = False
        trigger.last_outcome = 'expired'
        trigger.last_error = 'The schedule\'s end date has passed.'
        fields.append('enabled')
        outcome = 'expired'

    if fired:
        trigger.last_fired_at = now
        fields.append('last_fired_at')
    trigger.save(update_fields=fields)
    return outcome


def _clear_queue(trigger) -> None:
    if trigger.queued_for is not None:
        trigger.queued_for = None
        trigger.save(update_fields=['queued_for', 'updated_at'])


def fire(trigger, now: datetime | None = None) -> str:
    """
    Run one trigger's agent, and report what happened in a word.

    Returns one of `fired`, `queued`, `dropped`, `skipped`, `late`, `busy`,
    `waiting`, `expired`, `stopped`, `refused`, `failed` — the sweep counts
    these, and a caller reading the counts can tell "nothing was due" apart
    from "everything was refused", which a boolean cannot. The word is also
    written to
    `last_outcome` so the same distinction survives until someone looks at the
    schedule, rather than living only in a log line nobody has access to.
    """
    from agents.agent.runtime import AgentRunRefused, start_agent_run

    now = now or timezone.now()

    # A schedule outside its own window is neither broken nor due. `pending`
    # re-arms and waits; `expired` closes the row, because the alternative is
    # an enabled schedule that will never fire again and does not say so.
    state = trigger.window_state(now)
    if state == 'pending':
        return _rearm(trigger, now, 'waiting',
                      f'Starts {trigger.starts_at:%Y-%m-%d %H:%M} UTC.')
    if state == 'expired':
        trigger.enabled = False
        trigger.queued_for = None
        trigger.last_outcome = 'expired'
        trigger.last_error = 'The schedule\'s end date has passed.'
        trigger.save(update_fields=[
            'enabled', 'queued_for', 'last_outcome', 'last_error', 'updated_at',
        ])
        return 'expired'

    # A firing already owed from an earlier slot takes precedence over the
    # upcoming one: it is the older debt, and running both would double up.
    owed = trigger.queued_for
    if owed and (now - owed).total_seconds() > QUEUE_TTL_SECONDS:
        logger.warning('[Sweep] Trigger %s dropped a queued firing from %s.',
                       trigger.id, owed)
        _clear_queue(trigger)
        owed = None
        if trigger.next_due_at is None or trigger.next_due_at > now:
            # The queue was the only reason this row was in the sweep at all.
            # Falling through here would run the *next* slot early — a stale
            # 09:00 firing dropped at midnight would immediately trigger
            # tomorrow's, which is worse than the lateness it was avoiding.
            trigger.last_outcome = 'dropped'
            trigger.last_error = (
                'A firing that had been waiting for a free slot was dropped: '
                'it was more than six hours old.'
            )
            trigger.save(update_fields=['last_outcome', 'last_error', 'updated_at'])
            return 'dropped'

    due = owed or trigger.next_due_at
    if due and not owed and (now - due).total_seconds() > MAX_LATENESS_SECONDS:
        # Missed by more than the grace window — re-arm rather than run a
        # backlog. Firing every missed 9am after an outage is a stampede.
        logger.warning('[Sweep] Trigger %s is too late; skipping to next slot.',
                       trigger.id)
        return _rearm(trigger, now, 'late',
                      'Missed by more than an hour, so this firing was skipped.')

    if trigger.overlap != 'cancel' and _is_busy(trigger):
        if trigger.overlap == 'queue':
            # Owe it, and re-arm the next slot in the same write. Before this
            # column existed `queue` fell through and ran immediately, which is
            # the one thing all three policies agree must not happen.
            trigger.queued_for = due or now
            trigger.save(update_fields=['queued_for', 'updated_at'])
            # `_rearm` may find there is no next slot at all and close the
            # row; its word wins over ours, or the outcome would claim a
            # firing is waiting on a schedule that just stopped.
            return _rearm(trigger, now, 'queued',
                          'A run was still going, so this firing is waiting '
                          'its turn.')
        return _rearm(trigger, now, 'busy',
                      'A previous run was still going and the overlap policy '
                      'is "skip".')
    if trigger.overlap == 'cancel':
        _cancel_running(trigger)

    agent = trigger.subagent
    goal = (trigger.goal or agent.prompt or '').strip()
    if not goal:
        logger.warning('[Sweep] Trigger %s has nothing to ask the agent.', trigger.id)
        _clear_queue(trigger)
        return _rearm(trigger, now, 'skipped',
                      'This trigger has no goal and its agent has no brief, so '
                      'there is no instruction to run.')

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
        _note_failure(trigger, now, 'refused', str(exc))
        return 'refused'
    except Exception as exc:  # noqa: BLE001
        logger.exception('[Sweep] Trigger %s failed to start', trigger.id)
        _note_failure(trigger, now, 'failed', f'{type(exc).__name__}: {exc}')
        return 'failed'

    trigger.consecutive_failures = 0
    trigger.queued_for = None
    trigger.save(update_fields=['consecutive_failures', 'queued_for', 'updated_at'])
    _rearm(trigger, now, 'fired', '', fired=True)
    return 'fired'


def _note_failure(trigger, now: datetime, outcome: str = 'failed',
                  error: str = '') -> None:
    trigger.consecutive_failures += 1
    trigger.queued_for = None
    trigger.next_due_at = next_run_after(trigger.cron, now, trigger.tz)
    trigger.last_outcome = outcome
    # The reason is stored, not just logged. A user watching a schedule fail
    # five times and switch itself off could previously find out *that* it had
    # happened and never *why*, because the only copy of the reason was a
    # server log line they cannot read.
    trigger.last_error = error[:2000]
    fields = ['consecutive_failures', 'queued_for', 'next_due_at',
              'last_outcome', 'last_error', 'updated_at']

    if trigger.consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
        trigger.enabled = False
        fields.append('enabled')
        logger.error(
            '[Sweep] Trigger %s disabled after %s consecutive failures.',
            trigger.id, trigger.consecutive_failures,
        )

    trigger.save(update_fields=fields)


def due_triggers(now: datetime | None = None):
    """Every schedule the sweep would act on right now.

    Extracted so `manage.py run_due_triggers --dry-run` asks the same question
    the sweep does. It previously kept its own copy of this filter, which is
    the arrangement where a dry run reports "nothing due" and the real sweep
    fires — the two drifted the moment `queued_for` was added.

    Two questions, not one: what is due, and what is still owed from a slot
    `overlap='queue'` deferred. Both halves are indexed, and a row matching
    both is returned once — `fire` settles the older debt first.
    """
    from django.db.models import Q

    from agents.models import Trigger

    now = now or timezone.now()
    return (
        Trigger.objects
        .filter(enabled=True, mode='schedule')
        .filter(Q(next_due_at__isnull=False, next_due_at__lte=now)
                | Q(queued_for__isnull=False))
        .select_related('subagent', 'subagent__user')
    )


def run_trigger_sweep(now: datetime | None = None) -> dict[str, int]:
    """
    Fire every schedule trigger that is due. Safe to run more often than the
    schedule: a trigger is re-armed the moment it fires, so a second sweep in
    the same minute finds nothing due.
    """
    now = now or timezone.now()
    counts: dict[str, int] = {}

    for trigger in due_triggers(now):
        outcome = fire(trigger, now)
        counts[outcome] = counts.get(outcome, 0) + 1

    if counts:
        logger.info('[Sweep] %s', ', '.join(f'{v} {k}' for k, v in sorted(counts.items())))
    return counts
