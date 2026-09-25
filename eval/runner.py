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

from . import graders, supervision, workspace

logger = logging.getLogger(__name__)


class NoCasesToRun(ValueError):
    """The suite has nothing active in it. Refused rather than scored 0/0."""


class _SweepCeilingReached(RuntimeError):
    """The suite's `max_cost_rupees` was reached. Skips the rest, fails the run."""


def _goal_for(case, workspace_path: str = '') -> str:
    """The prompt one case hands the agent.

    `input_data` is appended as labelled JSON rather than merged into the
    sentence: the agent has to be able to tell the instruction from the data,
    and a case that interpolates its fixtures into prose is a case whose
    failures are about phrasing.

    Keys starting `__` are harness instructions (the workspace spec), never
    shown to the agent: its files are in its folder, not pasted into its prompt.
    `{workspace}` in the goal becomes the path the agent's file tools accept.
    """
    goal = (case.goal or '').strip()
    if workspace_path:
        goal = goal.replace('{workspace}', workspace_path)
    payload = {k: v for k, v in (case.input_data or {}).items() if not str(k).startswith('__')}
    if not payload:
        return goal
    return f'{goal}\n\nINPUT DATA (JSON):\n{json.dumps(payload, indent=2, default=str)}'


@sync_to_async
def open_run(suite, agent, user, notes: str = '', *, mode: str = 'agent'):
    """Create the `EvalRun` a sweep will fill in. Public on purpose.

    `start_suite_run` opens the run *before* spawning so the caller gets a
    subscribable id at once, and `run_suite_now` needs the same seam to await a
    sweep instead. A caller that has to reach for an underscore-prefixed helper
    to do either is a caller the module forgot to serve.
    """
    from logs import revisions

    from .environment import live_world
    from .models import EvalRun

    try:
        world = live_world(suite)
    except Exception:  # noqa: BLE001 - a world lookup must not block a sweep
        world = None

    kwargs: dict = {}
    if hasattr(EvalRun, 'mode'):
        kwargs['mode'] = mode
    if hasattr(EvalRun, 'world_version'):
        kwargs['world_version'] = world.version if world is not None else None
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
        **kwargs,
    )


_UNSET = object()


@sync_to_async
def _pinned_world(run, suite):
    """The world this sweep runs in: the version `open_run` recorded, loaded
    once. Every case uses this row, so accepting a new world mid-sweep can
    neither move the remaining cases onto it nor mix two versions under the
    one `EvalRun.world_version` the scorecard reports."""
    from .models import EvalWorld

    version = getattr(run, 'world_version', None)
    if version is None:
        return None
    return EvalWorld.objects.filter(suite=suite, version=version).first()


@sync_to_async
def _active_cases(suite, world=_UNSET) -> list:
    """Cases this sweep will run: active, and built for the live world.

    A case whose `world_version` is not the live world's is stale — kept and
    listed, never swept — because regenerating the world invalidates the
    cases built on the old one. Cases with no `world_version` (run imports,
    config-only drafts, cases predating worlds) are kept and run outside the
    world (`environment.for_attempt`). `world` is the sweep's pinned world;
    omitted, the live one is looked up (the pre-flight count).
    """
    from .environment import live_world

    cases = list(suite.cases.filter(is_active=True).order_by('order', 'id'))
    if world is _UNSET:
        try:
            world = live_world(suite)
        except Exception:  # noqa: BLE001
            world = None
    if world is None:
        return cases
    return [c for c in cases
            if c.world_version is None or c.world_version == world.version]


@sync_to_async
def _stale_case_count(suite) -> int:
    """Active cases excluded from the sweep for belonging to an old world."""
    from .environment import live_world

    try:
        world = live_world(suite)
    except Exception:  # noqa: BLE001
        return 0
    if world is None:
        return 0
    return suite.cases.filter(is_active=True).exclude(
        world_version__in=(None, world.version)).count()


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
    from django.utils import timezone as _tz

    from .models import EvalRun

    for key, value in fields.items():
        setattr(result, key, value)
    supervision.apply_policy(result, suite)
    result.save()
    # Touch the run so a long but live sweep is never mistaken for dead by
    # `eval/recovery.py::sweep_orphaned_eval_runs` (which keys off `updated_at`).
    EvalRun.objects.filter(pk=result.run_id).update(updated_at=_tz.now())


@sync_to_async
def _run_status(run) -> str:
    from .models import EvalRun

    return EvalRun.objects.filter(pk=run.pk).values_list('status', flat=True).first() or ''


@sync_to_async
def _run_spend_rupees(run) -> int:
    """What this sweep has spent so far, in rupees (agent + judge)."""
    from decimal import Decimal

    from django.db.models import Sum

    from agents.spend import rupees_for_usd

    from .models import EvalResult
    from logs.models import ExecutionLog
    from agents.spend import aggregate_rupees

    execution_ids = list(
        EvalResult.objects.filter(run_id=run.pk, execution__isnull=False)
        .values_list('execution_id', flat=True)
    )
    agent_rupees = 0
    if execution_ids:
        agent_rupees = aggregate_rupees(
            ExecutionLog.objects.filter(id__in=execution_ids)
        )
    judge_total = (
        EvalResult.objects.filter(run_id=run.pk)
        .aggregate(total=Sum('judge_cost_usd'))['total']
        or Decimal('0')
    )
    return agent_rupees + rupees_for_usd(judge_total)


@sync_to_async
def _suite_ceiling(suite) -> int | None:
    try:
        suite.refresh_from_db(fields=['max_cost_rupees'])
    except Exception:  # noqa: BLE001
        pass
    return getattr(suite, 'max_cost_rupees', None)


@sync_to_async
def _finish(run, *, status: str | None = None, error: str = '', tokens: int = 0):
    from django.db.models import Sum

    run.refresh_from_db()
    judge_tokens = (
        run.results.aggregate(total=Sum('judge_tokens'))['total'] or 0
    )
    run.tokens_used = tokens + int(judge_tokens or 0)
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


async def _run_case(run, suite, case, agent, user, sem, abort: asyncio.Event,
                    world=None) -> int:
    """Execute and grade one case. Returns the tokens it spent."""
    from agents.agent.runtime import AgentRunRefused, run_agent
    from llm.access import LLMUserActionable

    result = await _open_result(run, case)

    async with sem:        # Checked after acquiring, not before: with a concurrency of 2 and 200
        # cases, 198 of them are queued behind the semaphore when a refusal or
        # a cancel lands, and the check that matters is the one they make on
        # the way out of the queue.
        if abort.is_set() or await _run_status(run) == 'cancelled':
            abort.set()
            await _save_result(result, suite, status='skipped',
                               error_message='sweep stopped before this case ran')
            return 0

        # Sweep ceiling: a benchmark must never spend without bound. Checked
        # after acquiring the semaphore and before starting the case.
        # Null means unlimited; 0 means "already over" (useful in tests).
        ceiling = await _suite_ceiling(suite)
        if ceiling is not None:
            spent = await _run_spend_rupees(run)
            if spent >= ceiling:
                abort.set()
                await _save_result(
                    result, suite, status='skipped',
                    error_message=(
                        f'sweep cost ceiling reached (₹{spent} of ₹{ceiling})'
                    ),
                )
                raise _SweepCeilingReached(f'sweep cost ceiling reached (₹{spent} of ₹{ceiling})')

        started = time.monotonic()
        spec = workspace.spec_for(case)
        # An accepted world means this attempt runs inside it: confined
        # scopes, simulated dispatch, fresh fixtures. No world means the run
        # behaves exactly as today.
        from . import environment as envmod
        env = (await sync_to_async(envmod.for_attempt)(user, agent, suite, case, world)
               if world is not None else None)
        try:
            # Reset inside the semaphore and the try: a fixture that fails to
            # write is this case's error, and two attempts at the same case
            # must never share a half-written folder.
            if env is None:
                workspace_path = await workspace.prepare(user, agent, spec) if spec else ''
                agent_run = await run_agent(
                    agent, _goal_for(case, workspace_path), user=user,
                    trigger_type='api', caller='eval',
                    gated_calls=getattr(suite, 'gated_calls', 'run') or 'run',
                )
            else:
                workspace_path = await sync_to_async(env.prepare)(
                    case, spec, str(result.pk))
                agent_run = await run_agent(
                    agent, _goal_for(case, workspace_path), user=user,
                    trigger_type='api', caller='eval',
                    gated_calls=getattr(suite, 'gated_calls', 'run') or 'run',
                    environment=env,
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
    if env is None:
        files = await workspace.snapshot(user, agent, spec) if spec else {}
        binaries = await workspace.snapshot_binaries(user, agent, spec) if spec else {}
        env_snapshot: dict = {}
        env_changes: dict = {}
        allowed = _allowed_tools_for(agent)
    else:
        files, binaries = await sync_to_async(env.snapshot_files)()
        env_snapshot = env.snapshot_env()
        env_changes = env.changes(files)
        base_allowed = _allowed_tools_for(agent)
        if base_allowed is None:
            allowed = None
        else:
            withheld = env.withheld_names(set(base_allowed))
            allowed = [t for t in base_allowed if t not in withheld]
    ctx = graders.GradeContext(
        files=files,
        binaries=binaries,
        env=env_snapshot,
        code_changes=await _code_changes_for(agent_run.execution_id),
        allowed_tools=allowed,
        scope_claims=_scope_claims_for(case, spec),
        answer=answer,
        structured=agent_run.structured,
        contract_error=agent_run.contract_error,
        tool_trace=agent_run.tool_trace or [],
        tokens=agent_run.tokens,
        duration_ms=agent_run.duration_ms,
        # Eval runs record gated calls instead of pausing (`record_intents`),
        # so this is only reachable through something that still interrupts.
        # Reported as an error so `no_error` catches it instead of the empty
        # answer being graded as a bad one.
        error='run paused for approval' if agent_run.awaiting_approval else '',
        awaiting_approval=agent_run.awaiting_approval,
        intents=list(agent_run.intents or []),
        reasoning=agent_run.thinking or '',
        reference=case.reference or '',
        goal=case.goal or '',
        user_id=user.id,
    )
    grades, score, passed = await graders.grade_all(case.graders or [], ctx)

    execution = await _execution_for(agent_run.execution_id)
    truncated = len(answer) > EVAL_RESULT_ANSWER_CHAR_LIMIT
    judge_tokens = sum(int(getattr(g, 'tokens', 0) or 0) for g in grades)
    judge_cost = _sum_judge_cost(grades)
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
        judge_tokens=judge_tokens,
        judge_cost_usd=judge_cost,
        duration_ms=agent_run.duration_ms,
        error_message='',
        intents=list(agent_run.intents or []),
        env_changes=env_changes,
    )
    return agent_run.tokens + judge_tokens


def _sum_judge_cost(grades):
    from decimal import Decimal

    total = Decimal('0')
    seen = False
    for g in grades:
        cost = getattr(g, 'cost_usd', None)
        if cost is None:
            continue
        try:
            total += Decimal(str(cost))
            seen = True
        except Exception:  # noqa: BLE001
            continue
    return total if seen else None


@sync_to_async
def _execution_for(execution_id: str):
    from logs.models import ExecutionLog

    return ExecutionLog.objects.filter(execution_id=execution_id).first()


def _allowed_tools_for(agent) -> list[str] | None:
    """Built-in tool names this agent may call, for `disallowed_tool_used`.

    Mirrors `AgentToolbox.allowed_names` without the DB reads (native live
    check, browser/voice engines): an eval-time approximation that covers
    built-ins, which is what the grader judges. MCP `mcp__*` names are
    excluded by the grader itself. None means unrestricted (no agent, e.g.
    bare mode) so old suites never newly fail.
    """
    if agent is None:
        return None
    try:
        from agents.agent.runtime import ALWAYS_AVAILABLE, GRANT_TOOLS, RETRIEVAL_TOOLS
    except Exception:  # noqa: BLE001
        return None
    names = set(ALWAYS_AVAILABLE) | set(RETRIEVAL_TOOLS)
    grants = agent.tool_grants or {}
    for grant, tools in GRANT_TOOLS.items():
        if grants.get(grant):
            names.update(tools)
    scope = (agent.agent_context or {}).get('toolScope')
    if scope:
        names &= set(scope) | set(ALWAYS_AVAILABLE) | set(RETRIEVAL_TOOLS)
    perms = (agent.agent_context or {}).get('toolPermissions') or {}
    names -= {n for n, m in perms.items() if m == 'deny'}
    return sorted(names)


def _scope_claims_for(case, spec: dict | None) -> list[str]:
    """File globs this case allows writes to, for `scope_respected`.

    Today: the workspace root (everything under it is in scope). Tomorrow:
    per-case `claims` in `input_data`. Empty = unknown, and the grader passes.
    """
    claims: list[str] = []
    try:
        data_claims = (case.input_data or {}).get('claims')
        if isinstance(data_claims, str) and data_claims.strip():
            claims.append(data_claims.strip())
        elif isinstance(data_claims, list):
            claims.extend(str(c) for c in data_claims if str(c).strip())
    except Exception:  # noqa: BLE001
        pass
    if spec and spec.get('root'):
        root = str(spec['root']).strip('/')
        if root:
            claims.append(f'{root}/**')
    return claims


@sync_to_async
def _code_changes_for(execution_id: str) -> tuple[str, ...]:
    """Project-relative paths one run changed, for the claims grader.

    Read from the `CodeChange` rows the `shell` tools wrote — the same record
    the UI shows and `revert_task` reverts — so a run graded as "within its
    claims" is one whose real diff was, not one that said so.
    """
    from workspaces.models import CodeChange

    return tuple(CodeChange.objects.filter(
        run__execution_id=execution_id
    ).values_list('path', flat=True))


async def sweep(run, suite, agent, user, *, case_ids: list[int] | None = None,
              mode: str = 'agent') -> None:
    """Run every active case, then settle the run. Never raises to its caller.

    `case_ids` narrows the sweep to one case (the smoke gate's retry) without
    creating rows. `mode='bare'` runs the platform-tax control (Phase 8): one
    bare model call per case, no agent, no `ExecutionLog`.
    """
    world = await _pinned_world(run, suite)
    cases = await _active_cases(suite, world)
    if case_ids is not None:
        wanted = set(case_ids)
        cases = [c for c in cases if c.pk in wanted]
    if mode == 'bare':
        from . import bare as _bare

        await _bare.sweep_bare(run, suite, cases, agent, user)
        return
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
            *(_run_case(run, suite, case, agent, user, sem, abort, world)
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


async def run_suite_now(suite, agent, user, *, notes: str = '',
                        case_ids: list[int] | None = None,
                        mode: str = 'agent'):
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
    run = await open_run(suite, agent, user, notes, mode=mode)
    await sweep(run, suite, agent, user, case_ids=case_ids, mode=mode)
    return await _reload(run)


async def start_suite_run(suite, agent, user, *, notes: str = '') -> str:
    """Open a run, start the sweep in the background, return its id at once.

    Preflight happens **before** the run row exists, so a suite pointed at an
    agent with no credential answers 402 at the view instead of 202 followed by
    a sweep that dies on its first case. Same rule as
    `agents.agent.runtime.start_agent_run` — see the fail-before-you-look-busy
    note in CLAUDE.md.
    """
    from agents.agent.runtime import check_guardrails, resolve_agent_model
    from llm import access as llm
    from workflow_backend.background import spawn

    cases = await _active_cases(suite)
    if not cases:
        stale = await _stale_case_count(suite)
        hint = (f' {stale} active case(s) belong to an older world version — '
                f'regenerate the world or rebuild them.') if stale else ''
        raise NoCasesToRun(
            f'"{suite.name}" has no active cases. Add one before running it.{hint}'
        )

    await check_guardrails(agent, user)
    provider, model = await resolve_agent_model(agent, user)
    await llm.preflight(
        provider=provider,
        model=model,
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
