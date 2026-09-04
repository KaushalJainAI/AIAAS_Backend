"""
Runs that outlived nothing: picking up what a restart dropped.

A run is a detached task (`background.spawn`). When the process holding it goes
away — a deploy, a crash, an OOM kill — the task goes with it and *nothing
notices*. The `ExecutionLog` row stays `running` for ever, the user watches a
stream attached to no producer, and the agent's own stats count a run that
never ended. Nothing errors, which is why this had to be swept for rather than
caught.

**How an orphan is recognised.** Not by tracking process ids or boot times,
which stop working the moment there is more than one worker. A run declares its
own wall-clock limit up front (`guardrails['maxRunSeconds']`, enforced by
`agents/budget.py`), and the runtime kills it at that limit plus a wrap-up
grace. So a `running` row older than its own limit plus a margin is, by the
runtime's own rules, impossible — either the process died, or something ignored
a deadline it was supposed to honour. Both need exactly this treatment, and the
test needs no state beyond the row itself.

**What happens to one.** It depends on something this module must ask rather
than assume: whether the checkpointer is durable. With a durable saver the
graph state is still on disk, so the run is resumed from where it stopped and
keeps its execution id — the same reasoning `resume_agent_run` gives for a
paused run, that a trace split across two ids cannot be joined. With
`memory` the state died with the process, and the honest outcome is to fail the
row with a message saying so. What must not happen is a resume that silently
starts the work again from the beginning, having already charged the user for
the first attempt.

Reachable two ways, like every other sweep here (`agents/sweep.py`,
`notifications/reminders.py`, `inference/recycle.py`) and for the same reason:
local development has no Redis, and a beat-only recovery would fail by silently
never running — which is precisely the failure it exists to fix.
"""
from __future__ import annotations

import logging

from asgiref.sync import sync_to_async
from django.utils import timezone

logger = logging.getLogger(__name__)

#: Added to a run's own limit before it is called an orphan. Covers the
#: wrap-up grace, clock skew between workers, and a saver flushing its last
#: checkpoint. Generous on purpose: killing a run that was merely slow costs
#: the user work they have already paid for, while a stuck row costs a sweep
#: interval before it is noticed.
ORPHAN_GRACE_SECONDS = 300

#: Runs one sweep will touch. A backlog means something is badly wrong, and
#: resuming two hundred agent runs at once would finish the job the outage
#: started.
MAX_RECOVERIES_PER_SWEEP = 20


@sync_to_async
def _orphans(limit: int) -> list:
    """`running` rows that are past any limit they could legitimately have."""
    from agents.budget import clamp_run_seconds
    from logs.models import ExecutionLog

    now = timezone.now()
    candidates = list(
        ExecutionLog.objects
        .filter(status='running', started_at__isnull=False)
        .select_related('subagent', 'user')
        .order_by('started_at')[:limit * 5]
    )

    stale = []
    for log in candidates:
        agent = log.subagent
        if agent is None:
            # A run whose agent has been deleted can never be resumed; it is
            # closed below rather than left running for ever.
            stale.append((log, 0))
            continue
        allowed = clamp_run_seconds((agent.guardrails or {}).get('maxRunSeconds'))
        age = (now - log.started_at).total_seconds()
        if age > allowed + ORPHAN_GRACE_SECONDS:
            stale.append((log, allowed))
        if len(stale) >= limit:
            break
    return stale


@sync_to_async
def _fail(log, message: str) -> None:
    from logs.models import ExecutionLog

    # Re-read and re-check under the write: the sweep may be running beside the
    # very process that owns this run, and a row that finished in between must
    # not be overwritten with a failure.
    fresh = ExecutionLog.objects.filter(id=log.id, status='running').first()
    if fresh is None:
        return
    fresh.status = 'failed'
    fresh.error_message = message
    fresh.completed_at = timezone.now()
    if fresh.started_at:
        fresh.duration_ms = int(
            (fresh.completed_at - fresh.started_at).total_seconds() * 1000
        )
    fresh.save(update_fields=['status', 'error_message', 'completed_at',
                              'duration_ms'])


async def _has_state(thread_id: str) -> bool:
    """Whether the checkpointer still holds anything for this thread.

    Asked before resuming rather than inferred from `is_durable()`: a durable
    backend that was switched on *after* the run started has no state for it,
    and resuming that would restart the work from scratch on a log that already
    reports partial progress.
    """
    if not thread_id:
        return False
    try:
        from chat.turn import checkpoints
        from chat.turn.agent import get_graph

        graph = get_graph()
        # This process may never have written a checkpoint — the sweep usually
        # runs in a *fresh* process, which is the whole point. Without this the
        # read hits a database with no tables and every orphan is reported as
        # having no state, which is exactly the wrong answer.
        await checkpoints.setup(graph.checkpointer)

        snapshot = await graph.aget_state(
            {'configurable': {'thread_id': thread_id}}
        )
        return bool(snapshot and snapshot.values.get('messages'))
    except Exception:  # noqa: BLE001
        logger.warning('[Recovery] Could not read state for thread %s', thread_id,
                       exc_info=True)
        return False


async def sweep_orphaned_runs(limit: int = MAX_RECOVERIES_PER_SWEEP) -> dict:
    """Resume or close every run whose process is gone. Returns a tally."""
    from chat.turn import checkpoints

    tally = {'checked': 0, 'resumed': 0, 'failed': 0}

    for log, allowed in await _orphans(limit):
        tally['checked'] += 1
        thread_id = (log.input_data or {}).get('thread_id') or ''

        if log.subagent is None:
            await _fail(log, 'The agent for this run no longer exists.')
            tally['failed'] += 1
            continue

        if not checkpoints.is_durable() or not await _has_state(thread_id):
            await _fail(
                log,
                'This run was interrupted — the server restarted while it was '
                'working, and its progress was not saved. Start it again.'
                if not checkpoints.is_durable() else
                'This run was interrupted and no saved state was found for it. '
                'Start it again.',
            )
            tally['failed'] += 1
            logger.warning('[Recovery] Closed orphaned run %s (age > %ss)',
                           log.execution_id, allowed)
            continue

        try:
            resumed = await _resume(log, thread_id)
        except Exception:  # noqa: BLE001
            logger.exception('[Recovery] Could not resume run %s', log.execution_id)
            resumed = False

        if resumed:
            tally['resumed'] += 1
            logger.info('[Recovery] Resumed orphaned run %s', log.execution_id)
        else:
            await _fail(log, 'This run was interrupted and could not be resumed.')
            tally['failed'] += 1

    return tally


async def _resume(log, thread_id: str) -> bool:
    """Re-enter a run on its original execution id.

    Deliberately goes through `run_agent` with the existing `log`, exactly as
    the approval path does. The graph picks up from its checkpoint, so the work
    already done is not repeated, and the trace stays on one execution id
    instead of being split across two that nothing can join.
    """
    from workflow_backend.background import spawn

    from .agent.runtime import run_agent

    goal = (log.input_data or {}).get('goal', '')
    agent, user = log.subagent, log.user
    if user is None:
        return False

    async def _go() -> None:
        try:
            await run_agent(agent, goal, user=user, thread_id=thread_id,
                            trigger_type=log.trigger_type or 'manual', log=log,
                            # Carried, not defaulted: a worker that resumes as
                            # depth 0 could delegate again past the bound, and
                            # a counter that does not survive an interruption
                            # is not a bound.
                            depth=log.depth or 0)
        except Exception:
            logger.exception('[Recovery] Resumed run %s failed', log.execution_id)

    spawn(_go(), name=f'agent-recover:{log.execution_id}')
    return True
