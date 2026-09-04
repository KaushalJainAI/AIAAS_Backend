"""
Sweeping a suite: run the agent once per case, grade what came back, then hand
whatever the graders could not settle to a person.

**One door, again.** Every case is executed through
`agents.agent.runtime.run_agent` with `caller='api'` — the same entry point a
user pressing "run" goes through, under the same guardrails, writing the same
`ExecutionLog`. An eval that ran the agent by some private path would be
measuring a code path nobody uses. `EvalResult.execution` is the FK that keeps
the full turn-by-turn trace one hop from the score.

**Refusals abort the sweep; failures do not.** A case whose agent run raises is
one errored result. A *guardrail* refusal — a spent cap, a missing credential —
means every remaining case would raise identically, so the sweep stops and says
why. Two hundred rows all reading "monthly spend cap reached" is not a report.

**Detached with `spawn()`.** A sweep outlives its HTTP response, so it uses
`workflow_backend.background.spawn()` for the reason spelled out there: a bare
`create_task` inherits the request's executor and dies with it, mid-ORM-call.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time

from asgiref.sync import sync_to_async
from django.utils import timezone

from workflow_backend.thresholds import (
    EVAL_MAX_CONCURRENCY,
    EVAL_RESULT_ANSWER_CHAR_LIMIT,
)

from . import graders, supervision

logger = logging.getLogger(__name__)


class NoCasesToRun(ValueError):
    """The suite has nothing active in it. Refused rather than scored 0/0."""


def _goal_for(case) -> str:
    """The prompt one case hands the agent.

    `input_data` is appended as labelled JSON rather than merged into the
    sentence: the agent has to be able to tell the instruction from the data,
    and a case that interpolates its fixtures into prose is a case whose
    failures are about phrasing.
    """
    goal = (case.goal or '').strip()
    payload = case.input_data or {}
    if not payload:
        return goal
    return f'{goal}\n\nINPUT DATA (JSON):\n{json.dumps(payload, indent=2, default=str)}'


@sync_to_async
def open_run(suite, agent, user, notes: str = ''):
    """Create the `EvalRun` a sweep will fill in. Public on purpose.

    `start_suite_run` opens the run *before* spawning so the caller gets a
    subscribable id at once, and `run_suite_now` needs the same seam to await a
    sweep instead. A caller that has to reach for an underscore-prefixed helper
    to do either is a caller the module forgot to serve.
    """
    from logs import revisions

    from .models import EvalRun

    return EvalRun.objects.create(
        suite=suite,
        subagent=agent,
        # Pinned at open time, not read back at close: an agent edited while
        # the sweep is running must not change what the sweep claims to have
        # scored. Same rule as `ExecutionLog.revision`.
        revision=revisions.current(agent) if agent is not None else None,
        user=user,
        status='running',
        supervision=suite.supervision,
        total_cases=0,
        started_at=timezone.now(),
        notes=notes or '',
    )


@sync_to_async
def _active_cases(suite) -> list:
    return list(suite.cases.filter(is_active=True).order_by('order', 'id'))


@sync_to_async
def _open_result(run, case):
    from .models import EvalResult

    return EvalResult.objects.create(
        run=run,
        case=case,
        case_name=(case.name or '')[:200],
        goal=case.goal or '',
        weight=float(case.weight or 1.0),
        status='running',
    )


@sync_to_async
def _save_result(result, suite, **fields):
    for key, value in fields.items():
        setattr(result, key, value)
    supervision.apply_policy(result, suite)
    result.save()


@sync_to_async
def _run_status(run) -> str:
    from .models import EvalRun

    return EvalRun.objects.filter(pk=run.pk).values_list('status', flat=True).first() or ''


@sync_to_async
def _finish(run, *, status: str | None = None, error: str = '', tokens: int = 0):
    run.refresh_from_db()
    run.tokens_used = tokens
    if status:
        run.status = status
    if error:
        run.error_message = error[:2000]
    if run.completed_at is None:
        run.completed_at = timezone.now()
        if run.started_at:
            run.duration_ms = int(
                (run.completed_at - run.started_at).total_seconds() * 1000
            )
    run.save(update_fields=['status', 'error_message', 'tokens_used',
                            'completed_at', 'duration_ms', 'updated_at'])
    # `recompute` owns status for a sweep that got through its cases; it leaves
    # 'failed' and 'cancelled' alone, so calling it here is safe for both.
    supervision.recompute(run)
    return run


async def _run_case(run, suite, case, agent, user, sem, abort: asyncio.Event) -> int:
    """Execute and grade one case. Returns the tokens it spent."""
    from agents.agent.runtime import AgentRunRefused, run_agent
    from llm.access import LLMUserActionable

    result = await _open_result(run, case)

    async with sem:
        # Checked after acquiring, not before: with a concurrency of 2 and 200
        # cases, 198 of them are queued behind the semaphore when a refusal or
        # a cancel lands, and the check that matters is the one they make on
        # the way out of the queue.
        if abort.is_set() or await _run_status(run) == 'cancelled':
            abort.set()
            await _save_result(result, suite, status='skipped',
                               error_message='sweep stopped before this case ran')
            return 0

        started = time.monotonic()
        try:
            agent_run = await run_agent(
                agent, _goal_for(case), user=user,
                trigger_type='api', caller='api',
            )
        except (AgentRunRefused, LLMUserActionable) as exc:
            # Every remaining case would fail the same way. Stop the sweep
            # rather than fill it with identical rows.
            abort.set()
            await _save_result(result, suite, status='error',
                               error_message=str(exc)[:2000],
                               duration_ms=int((time.monotonic() - started) * 1000))
            raise
        except Exception as exc:
            logger.exception('[Eval] case %s failed', case.pk)
            await _save_result(result, suite, status='error',
                               error_message=str(exc)[:2000],
                               duration_ms=int((time.monotonic() - started) * 1000))
            return 0

    answer = agent_run.answer or ''
    ctx = graders.GradeContext(
        answer=answer,
        structured=agent_run.structured,
        contract_error=agent_run.contract_error,
        tool_trace=agent_run.tool_trace or [],
        tokens=agent_run.tokens,
        duration_ms=agent_run.duration_ms,
        # A run paused for approval never produced a final answer. Reported as
        # an error condition to the graders so `no_error` catches it instead of
        # the empty answer being graded as a bad one.
        error='run paused for approval' if agent_run.awaiting_approval else '',
        reference=case.reference or '',
        goal=case.goal or '',
        user_id=user.id,
    )
    grades, score, passed = await graders.grade_all(case.graders or [], ctx)

    execution = await _execution_for(agent_run.execution_id)
    truncated = len(answer) > EVAL_RESULT_ANSWER_CHAR_LIMIT
    await _save_result(
        result, suite,
        status='graded',
        execution=execution,
        answer=answer[:EVAL_RESULT_ANSWER_CHAR_LIMIT],
        answer_truncated=truncated,
        auto_passed=passed,
        auto_score=score,
        grades=[g.as_dict() for g in grades],
        tokens=agent_run.tokens,
        duration_ms=agent_run.duration_ms,
        error_message='',
    )
    return agent_run.tokens


@sync_to_async
def _execution_for(execution_id: str):
    from logs.models import ExecutionLog

    return ExecutionLog.objects.filter(execution_id=execution_id).first()


async def sweep(run, suite, agent, user) -> None:
    """Run every active case, then settle the run. Never raises to its caller."""
    cases = await _active_cases(suite)
    concurrency = max(1, min(int(suite.concurrency or 1), EVAL_MAX_CONCURRENCY))
    sem = asyncio.Semaphore(concurrency)
    abort = asyncio.Event()

    tokens = 0
    refusal = ''
    try:
        # Every case is dispatched at once and the semaphore does the limiting.
        # A sequential `await` per case would make `suite.concurrency` a lie —
        # which it was, in the first cut of this file.
        outcomes = await asyncio.gather(
            *(_run_case(run, suite, case, agent, user, sem, abort)
              for case in cases),
            return_exceptions=True,
        )
        for outcome in outcomes:
            if isinstance(outcome, BaseException):
                # Only a refusal propagates this far; per-case failures are
                # already recorded as errored results. Keep the first one —
                # the later ones are its echo.
                refusal = refusal or str(outcome)
            else:
                tokens += outcome
    except Exception as exc:  # defensive: the sweep must always close its run
        logger.exception('[Eval] sweep of suite %s failed', suite.pk)
        refusal = str(exc)

    status = None
    if await _run_status(run) == 'cancelled':
        status = 'cancelled'
    elif refusal:
        status = 'failed'

    await _finish(run, status=status, error=refusal, tokens=tokens)

    if status is None:
        await sync_to_async(supervision.notify_reviewer)(await _reload(run))


@sync_to_async
def _reload(run):
    from .models import EvalRun

    return EvalRun.objects.select_related('suite', 'subagent').get(pk=run.pk)


async def run_suite_now(suite, agent, user, *, notes: str = ''):
    """Sweep a suite and **await** it, returning the settled `EvalRun`.

    The counterpart to `start_suite_run`, for callers that are not an HTTP
    request: a management command, a Celery task, another app's code, a test.
    Nothing is detached, so exceptions surface to the caller and the returned
    row is already recomputed.

    Deliberately skips the preflight `start_suite_run` does: a caller awaiting
    the result learns about a missing credential from the errored results, and
    duplicating the check here would mean two places could disagree about what
    refuses a run.
    """
    run = await open_run(suite, agent, user, notes)
    await sweep(run, suite, agent, user)
    return await _reload(run)


async def start_suite_run(suite, agent, user, *, notes: str = '') -> str:
    """Open a run, start the sweep in the background, return its id at once.

    Preflight happens **before** the run row exists, so a suite pointed at an
    agent with no credential answers 402 at the view instead of 202 followed by
    a sweep that dies on its first case. Same rule as
    `agents.agent.runtime.start_agent_run` — see the fail-before-you-look-busy
    note in CLAUDE.md.
    """
    from agents.agent.runtime import check_guardrails
    from llm import access as llm
    from workflow_backend.background import spawn

    cases = await _active_cases(suite)
    if not cases:
        raise NoCasesToRun(
            f'"{suite.name}" has no active cases. Add one before running it.'
        )

    await check_guardrails(agent, user)
    await llm.preflight(
        provider=agent.llm_provider or 'openrouter',
        model=agent.llm_model or '',
        user_id=user.id,
    )

    run = await open_run(suite, agent, user, notes)

    async def _go() -> None:
        try:
            await sweep(run, suite, agent, user)
        except Exception:
            logger.exception('[Eval] background sweep of run %s failed', run.run_id)

    spawn(_go(), name=f'eval-sweep:{run.run_id}')
    return str(run.run_id)


__all__ = ['NoCasesToRun', 'open_run', 'run_suite_now', 'start_suite_run', 'sweep']
