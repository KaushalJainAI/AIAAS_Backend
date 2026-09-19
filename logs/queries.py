"""
Every database read behind `/api/logs/`.

The views hold none of this: they validate query params, call one function here,
and return the result. Keeping the ORM in its own module means the query logic is
testable without a request, and the aggregation SQL is not buried two indents
inside an HTTP handler.

Everything here is synchronous and returns plain dicts/lists, ready to hand to
`Response` as-is.

**On vocabulary.** Responses keep the `workflow_id` / `workflow_name` keys even
though the column is `subagent`, because the frontend and BrowserOS ship their
own builds. The rename happens in `_execution_row`, in one place.
"""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from typing import Any

from django.core.exceptions import ValidationError as DjangoValidationError
from django.db.models import Avg, Count, Prefetch, Q, QuerySet, Sum
from django.db.models.functions import TruncDate
from django.utils import timezone

from core.http.pagination import paginate_keyset
from llm.pricing import combine_sources, format_usd
from workflow_backend.thresholds import (
    EXECUTION_NODE_LOG_LIMIT,
    EXECUTION_TURN_LIMIT,
    REVISION_TIMELINE_LIMIT,
)

from .models import AgentStep, AgentTurn, ExecutionLog, SubAgentRevision

_STEP_FIELDS = (
    'id', 'call_id', 'tool', 'status', 'order', 'duration_ms',
    'error_message', 'args', 'result', 'started_at', 'completed_at',
)


def _percent(part: int, whole: int) -> float:
    """Share of `whole` as a 0-100 percentage. A zero denominator reads as 0."""
    return round(part / whole * 100, 1) if whole else 0


def _since(days: int):
    return timezone.now() - timedelta(days=days)


def _stringify_dates(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """`TruncDate` yields `date` objects, `Sum` over money yields `Decimal`.

    Both are flattened here so every row leaving this module is plain JSON —
    a Decimal that reaches the renderer becomes a float, and a cost is the one
    number that must not acquire binary drift on the way out.
    """
    for row in rows:
        row['date'] = row['date'].isoformat() if row['date'] else None
        if 'cost_usd' in row:
            row['cost_usd'] = format_usd(row['cost_usd'])
    return rows


def _user_executions(user, *, days: int | None = None) -> QuerySet:
    qs = ExecutionLog.objects.filter(user=user)
    return qs.filter(created_at__gte=_since(days)) if days is not None else qs


# ======================== Insights ========================

def execution_statistics(user, *, days: int, agent_id: int | None = None) -> dict[str, Any]:
    """Run counts, success rate and daily trend over the last `days`.

    Eval sweeps (`caller='eval'`) are excluded: they are benchmark traffic, not
    user runs, and counting them would inflate runs and spend on the agent.
    """
    qs = _user_executions(user, days=days).exclude(caller='eval')
    if agent_id:
        qs = qs.filter(subagent_id=agent_id)

    # One GROUP BY instead of three COUNT queries: the totals and the per-status
    # breakdown are the same aggregate read two ways.
    by_status = dict(
        qs.values('status').annotate(count=Count('id')).values_list('status', 'count')
    )
    total = sum(by_status.values())
    completed = by_status.get('completed', 0)

    aggregates = qs.aggregate(
        avg_duration=Avg('duration_ms'),
        total_nodes=Sum('nodes_executed'),
        total_tokens=Sum('tokens_used'),
    )

    daily_trend = _stringify_dates(list(
        qs.annotate(date=TruncDate('created_at'))
        .values('date')
        .annotate(count=Count('id'), success=Count('id', filter=Q(status='completed')))
        .order_by('date')
        .values('date', 'count', 'success')
    ))

    return {
        "summary": {
            "total_executions": total,
            "successful": completed,
            "failed": by_status.get('failed', 0),
            "success_rate": _percent(completed, total),
            "avg_duration_ms": round(aggregates['avg_duration'] or 0),
            "total_nodes_executed": aggregates['total_nodes'] or 0,
            "total_tokens_used": aggregates['total_tokens'] or 0,
        },
        "by_status": by_status,
        "by_trigger": dict(
            qs.values('trigger_type')
            .annotate(count=Count('id'))
            .values_list('trigger_type', 'count')
        ),
        # Who started these runs. Distinguishes a run the user asked for from
        # one an agent decided to spend their credits on.
        "by_caller": dict(
            qs.values('caller').annotate(count=Count('id')).values_list('caller', 'count')
        ),
        "daily_trend": daily_trend,
    }


def agent_metrics(user, agent_id: int) -> dict[str, Any] | None:
    """Per-agent detail. None when the agent does not exist or is not theirs."""
    from agents.models import SubAgent

    agent = SubAgent.objects.filter(id=agent_id, user=user).first()
    if agent is None:
        return None

    executions = ExecutionLog.objects.filter(subagent=agent).exclude(caller='eval')
    total = executions.count()
    completed = executions.filter(status='completed').count()
    aggregates = executions.aggregate(
        avg_duration=Avg('duration_ms'), total_tokens=Sum('tokens_used')
    )

    steps = AgentStep.objects.filter(execution__subagent=agent).exclude(execution__caller='eval')
    tool_stats = steps.values('tool').annotate(
        total=Count('id'), success=Count('id', filter=Q(status='completed'))
    )

    return {
        "workflow_id": agent_id,
        "workflow_name": agent.name,
        "total_executions": total,
        "avg_duration_ms": round(aggregates['avg_duration'] or 0),
        "success_rate": _percent(completed, total),
        "total_tokens_used": aggregates['total_tokens'] or 0,
        "revision_count": SubAgentRevision.objects.filter(subagent=agent).count(),
        "recent_executions": [
            _execution_row(row)
            for row in executions.select_related('subagent').order_by('-created_at')[:10]
        ],
        # Keyed by tool name, because that is the unit an agent actually has:
        # "knowledge_base_search fails half the time" is actionable, and it is
        # the reading the old node-keyed version could not give.
        "tool_success_rates": {
            row['tool']: {
                "success_rate": _percent(row['success'], row['total']),
                "total_runs": row['total'],
            }
            for row in tool_stats
        },
        # Tools that fail most often.
        "error_hotspots": list(
            steps.filter(status='failed')
            .values('tool')
            .annotate(error_count=Count('id'))
            .order_by('-error_count')[:5]
        ),
    }


def _source_of(total: int, unpriced: int, billed: int) -> str:
    """The honest label for a sum of `total` runs, from two counts.

    `combine_sources` needs every part; a GROUP BY hands back counts, so this
    applies the same rule to them: any unpriced part makes the sum unpriced,
    and only a sum that is billed throughout is billed. `by_workflow` used to
    label every priced agent `estimated`, including ones OpenRouter had billed.
    """
    if not total:
        return 'unpriced'
    if unpriced:
        return 'unpriced'
    return 'billed' if billed == total else 'estimated'


def _chat_spend(user, *, days: int) -> dict[str, Any]:
    """What conversations cost, which Insights used to leave out entirely.

    Chat is priced per message (`ChatMessage.cost_usd`), not per run, so it
    has no `ExecutionLog` and none of the sums above could see it — for most
    users that was most of their spend. Only assistant rows carry a cost.
    """
    from chat.models import ChatMessage

    rows = ChatMessage.objects.filter(
        session__user=user, role='assistant', created_at__gte=_since(days),
    )
    agg = rows.aggregate(
        cost=Sum('cost_usd'),
        tokens=Sum('input_tokens'),
        output=Sum('output_tokens'),
        total=Count('id'),
        unpriced=Count('id', filter=Q(cost_source__in=('', 'unpriced'))),
        billed=Count('id', filter=Q(cost_source='billed')),
        platform=Count('id', filter=Q(paid_by='platform')),
        own_key=Count('id', filter=Q(paid_by='own_key')),
    )
    return {
        "messages": agg['total'] or 0,
        "tokens": (agg['tokens'] or 0) + (agg['output'] or 0),
        "cost_usd": format_usd(agg['cost']),
        "cost_source": _source_of(agg['total'] or 0, agg['unpriced'] or 0,
                                  agg['billed'] or 0),
        # How many answers each kind of key paid for, so a figure can be
        # read as "your bill" or "our bill" rather than guessed at.
        "paid_by": {
            "platform": agg['platform'] or 0,
            "own_key": agg['own_key'] or 0,
        },
    }


def cost_breakdown(user, *, days: int) -> dict[str, Any]:
    """Token and credit usage over the last `days`, split by agent and tool.

    Eval traffic is real money, so it is included — but reported as its own
    `by_caller['eval']` line, so benchmark spend never hides inside an agent's
    figure.
    """
    executions = _user_executions(user, days=days)
    totals = executions.aggregate(
        total_tokens=Sum('tokens_used'),
        total_cost_usd=Sum('cost_usd'),
        total_input=Sum('input_tokens'),
        total_output=Sum('output_tokens'),
        total_cached_read=Sum('cached_read_tokens'),
        total_runs=Count('id'),
        unpriced_runs=Count('id', filter=Q(cost_source='unpriced')),
        billed_runs=Count('id', filter=Q(cost_source='billed')),
    )
    chat = _chat_spend(user, days=days)
    agents_source = _source_of(totals['total_runs'] or 0,
                               totals['unpriced_runs'] or 0,
                               totals['billed_runs'] or 0)

    return {
        "period_days": days,
        "total_tokens": totals['total_tokens'] or 0,
        # `credits_used` was summed here and nothing has ever written it, so
        # this endpoint's headline number was structurally zero. Kept on the
        # wire at zero for one release so an old client does not read `None`.
        "total_credits": 0,
        # Agent runs only, as it always was — `chat` and `all_cost_usd` below
        # are the additions, so an older client reading this is not misled
        # into thinking the meaning moved under it.
        "total_cost_usd": format_usd(totals['total_cost_usd']),
        "total_cost_source": agents_source,
        "chat": chat,
        # Agents and chat together, labelled by the weaker of the two: a total
        # with an unpriced part is not a number to state confidently.
        "all_cost_usd": format_usd(
            (totals['total_cost_usd'] or Decimal('0'))
            + Decimal(chat['cost_usd'] or '0')
        ),
        "all_cost_source": combine_sources(
            [s for s, n in ((agents_source, totals['total_runs']),
                            (chat['cost_source'], chat['messages'])) if n]
        ),
        "total_input_tokens": totals['total_input'] or 0,
        "total_output_tokens": totals['total_output'] or 0,
        "total_cached_read_tokens": totals['total_cached_read'] or 0,
        # Renamed from `subagent__id` / `subagent__name` so this response keeps
        # the same wire vocabulary as every other `/api/logs/` endpoint — the
        # column name must not leak out of the query layer.
        "by_workflow": [
            {
                "workflow_id": row['subagent__id'],
                # Runs outlive a deleted agent now, and all of them group
                # under one null id.
                "workflow_name": row['subagent__name'] or 'Deleted agents',
                "tokens": row['tokens'],
                "cost_usd": format_usd(row['cost_usd']),
                # An agent with even one unpriced run cannot have its total
                # reported as money: the sum silently omits that run, so a
                # figure would understate it while looking exact. Counted
                # rather than combined, because `combine_sources` needs the
                # individual values and this is one GROUP BY.
                "cost_source": _source_of(row['executions'], row['unpriced'],
                                          row['billed']),
                "executions": row['executions'],
            }
            for row in executions.values('subagent__id', 'subagent__name')
            .annotate(
                tokens=Sum('tokens_used'),
                cost_usd=Sum('cost_usd'),
                executions=Count('id'),
                unpriced=Count('id', filter=Q(cost_source='unpriced')),
                billed=Count('id', filter=Q(cost_source='billed')),
            )
            .order_by('-tokens')[:10]
        ],
        # Which *model* the money went to. `by_workflow` says which agent
        # spent it, which is a different question — one agent on an expensive
        # model and five on a cheap one look identical by agent and obvious
        # by model.
        "by_model": [
            {
                "model_id": row['model_id'] or 'unknown',
                "provider": row['provider'] or '',
                "tokens": row['tokens'] or 0,
                "cost_usd": format_usd(row['cost_usd']),
                "turns": row['turns'],
            }
            for row in AgentTurn.objects.filter(
                execution__user=user, execution__created_at__gte=_since(days)
            )
            .values('model_id', 'provider')
            .annotate(
                tokens=Sum('tokens'),
                cost_usd=Sum('cost_usd'),
                turns=Count('id'),
            )
            .order_by('-cost_usd')[:10]
        ],
        "by_tool": list(
            AgentStep.objects.filter(
                execution__user=user, execution__created_at__gte=_since(days)
            )
            .values('tool')
            .annotate(count=Count('id'))
            .order_by('-count')
        ),
        "by_caller": dict(
            executions.values('caller').annotate(count=Count('id')).values_list('caller', 'count')
        ),
        "daily_usage": _stringify_dates(list(
            executions.annotate(date=TruncDate('created_at'))
            .values('date')
            .annotate(tokens=Sum('tokens_used'), cost_usd=Sum('cost_usd'))
            .order_by('date')
        )),
    }


# ======================== Execution history ========================

def execution_page(
    user,
    *,
    limit: int,
    cursor: str | None = None,
    agent_id: int | None = None,
    status: str | None = None,
    caller: str | None = None,
) -> dict[str, Any]:
    """One keyset-paginated page of the user's runs, newest first.

    Eval sweeps are hidden by default and shown only when the caller
    explicitly passes `caller='eval'` — benchmark traffic must not pollute
    `/runs`.
    """
    qs = ExecutionLog.objects.filter(user=user)
    if agent_id:
        qs = qs.filter(subagent_id=agent_id)
    if status:
        qs = qs.filter(status=status)
    if caller:
        qs = qs.filter(caller=caller)
    else:
        qs = qs.exclude(caller='eval')

    # Counting is the expensive half, and a caller paging forward already has
    # the total from its first request - so only the uncursored call pays for it.
    total = qs.count() if not cursor else None

    # `paginate_keyset` needs model instances (it reads `.id` and the sort field
    # off the last row to mint the cursor), so the projection to the wire shape
    # happens here rather than via `.values()`.
    page = paginate_keyset(qs.select_related('subagent', 'revision'),
                           limit=limit, cursor=cursor)

    return {
        "count": total,
        "results": [_execution_row(row) for row in page.items],
        "limit": limit,
        "next_cursor": page.next_cursor,
        "has_more": page.has_more,
    }


def _agent_name(execution: ExecutionLog) -> str | None:
    """The agent's name, surviving the agent.

    A deleted agent's runs are kept (the FK is SET_NULL), and a run listed as
    "unknown" is little better than one erased. The revision it executed under
    is a full config snapshot, name included, so it answers for the agent.
    """
    if execution.subagent_id and execution.subagent:
        return execution.subagent.name
    revision = getattr(execution, 'revision', None)
    name = (revision.config or {}).get('name') if revision else None
    return f'{name} (deleted)' if name else None


def _execution_row(execution: ExecutionLog) -> dict[str, Any]:
    return {
        "id": execution.id,
        "execution_id": str(execution.execution_id),
        "workflow_id": execution.subagent_id,
        "workflow_name": _agent_name(execution),
        "status": execution.status,
        "trigger_type": execution.trigger_type,
        "caller": execution.caller,
        "depth": execution.depth,
        "is_delegated": execution.parent_step_id is not None,
        "duration_ms": execution.duration_ms,
        "nodes_executed": execution.nodes_executed,
        "tokens_used": execution.tokens_used,
        # The breakdown rides alongside the total rather than replacing it: a
        # client that only knows about `tokens_used` keeps working, and one
        # that wants to show what the run cost has the four buckets it was
        # actually billed in. `cost_source` is what stops a `0.00` that means
        # "free" rendering identically to one that means "we do not know".
        "input_tokens": execution.input_tokens,
        "output_tokens": execution.output_tokens,
        "cached_read_tokens": execution.cached_read_tokens,
        "cached_write_tokens": execution.cached_write_tokens,
        "cost_usd": format_usd(execution.cost_usd),
        "cost_source": execution.cost_source or "unpriced",
        "error_message": execution.error_message,
        "failure_category": execution.failure_category,
        "started_at": execution.started_at,
        "completed_at": execution.completed_at,
        "created_at": execution.created_at,
    }


def _step_row(step: AgentStep) -> dict[str, Any]:
    return {
        "id": step.id,
        "call_id": step.call_id,
        "tool": step.tool,
        "status": step.status,
        "order": step.order,
        "duration_ms": step.duration_ms,
        "error_message": step.error_message,
        "args": step.args,
        "result": step.result,
        "started_at": step.started_at,
        "completed_at": step.completed_at,
        # Runs this step delegated. Present only on `invoke_subagent` /
        # `run_agent` steps, which is what makes the delegation tree walkable
        # downward from the orchestrator's own trace.
        "delegated_runs": [
            {
                "execution_id": str(child.execution_id),
                "workflow_id": child.subagent_id,
                "workflow_name": _agent_name(child),
                "status": child.status,
                "task": child.delegation_task,
                "index": child.delegation_index,
                "tokens_used": child.tokens_used,
                "duration_ms": child.duration_ms,
            }
            for child in sorted(
                step.delegated_runs.all(), key=lambda c: c.delegation_index
            )
        ],
    }


def execution_detail(user, execution_id: str) -> dict[str, Any] | None:
    """One run as the loop it actually was: turns, each holding its own steps.

    None when the run does not exist or is not theirs.
    """
    try:
        execution = (
            ExecutionLog.objects
            .select_related('subagent', 'revision', 'parent_step__execution__subagent',
                            'parent_step__turn')
            .filter(execution_id=execution_id, user=user)
            .first()
        )
    except (DjangoValidationError, ValueError):
        # `execution_id` is a UUIDField and the URL captures a free `str`, so a
        # malformed id raises rather than missing. That is a 404, not a 500.
        return None
    if execution is None:
        return None

    # Bounded: each step carries its own args/result JSON, so a long run made
    # this response grow without limit — the caller asked for one execution and
    # got every payload the run ever produced. The cap is applied across the
    # whole run and then distributed into turns, so a truncated response is
    # still a prefix of the real trace rather than a sample of it.
    step_total = AgentStep.objects.filter(execution=execution).count()
    steps = list(
        AgentStep.objects.filter(execution=execution)
        .select_related('turn')
        .prefetch_related(
            Prefetch(
                'delegated_runs',
                queryset=ExecutionLog.objects.select_related('subagent', 'revision'),
            )
        )
        .order_by('order')[:EXECUTION_NODE_LOG_LIMIT]
    )

    steps_by_turn: dict[int | None, list[AgentStep]] = {}
    for step in steps:
        steps_by_turn.setdefault(step.turn_id, []).append(step)

    # Turns are capped independently of steps: each carries its full reasoning,
    # and the steps cap alone would not bound a run with thousands of turns.
    turn_total = AgentTurn.objects.filter(execution=execution).count()
    turns = [
        {
            "index": turn.index,
            "decision": turn.decision,
            "reasoning": turn.reasoning,
            "reasoning_truncated": turn.reasoning_truncated,
            "content": turn.content,
            "content_truncated": turn.content_truncated,
            "provider": turn.provider,
            "model_id": turn.model_id,
            "tokens": turn.tokens,
            "input_tokens": turn.input_tokens,
            "output_tokens": turn.output_tokens,
            "cached_read_tokens": turn.cached_read_tokens,
            "cached_write_tokens": turn.cached_write_tokens,
            "reasoning_tokens": turn.reasoning_tokens,
            "cost_usd": format_usd(turn.cost_usd),
            "cost_source": turn.cost_source or "unpriced",
            "duration_ms": turn.duration_ms,
            "created_at": turn.created_at,
            "steps": [_step_row(s) for s in steps_by_turn.get(turn.id, [])],
        }
        for turn in AgentTurn.objects.filter(execution=execution).order_by('index')
        [:EXECUTION_TURN_LIMIT]
    ]

    # Steps written before their turn row existed — a backfilled historical run,
    # or a step whose turn write failed. Surfaced rather than dropped: an
    # unattributed step is still something the agent did.
    orphans = [_step_row(s) for s in steps_by_turn.get(None, [])]

    detail = {
        **_execution_row(execution),
        "credits_used": execution.credits_used,
        "supervision_level": execution.supervision_level,
        "failure_category": execution.failure_category,
        "input_data": execution.input_data,
        "output_data": execution.output_data,
        "error_node_id": execution.error_node_id,
        "turns": turns,
        "unattributed_steps": orphans,
        # A truncated list and a complete one must not look alike.
        "step_total": step_total,
        "steps_truncated": step_total > len(steps),
        "turn_total": turn_total,
        "turns_truncated": turn_total > len(turns),
        "revision": _revision_summary(execution.revision),
        "delegated_by": _delegated_by(execution),
        "feedback": feedback_for(user, execution=execution),
        **_delegated_cost(execution),
    }
    return detail


def _delegated_cost(execution: ExecutionLog) -> dict[str, Any]:
    """What this run cost once its delegated workers are counted.

    Separate from `cost_usd` rather than folded into it, because they answer
    different questions: an orchestrator's own spend is a rounding error next
    to its eight workers', and a single blended figure would hide which of the
    two a user is looking at. Walked one generation at a time — delegation
    nests, and `parent_step__execution` is the only link between the levels.

    Bounded by `MAX_DELEGATION_DEPTH` rather than trusting the tree to be
    shallow: this is a read on a page load, and a cycle (however impossible it
    should be) must cost a wrong number, not the request.
    """
    from django.db.models import Sum

    from agents.agent.orchestrator import MAX_DELEGATION_DEPTH

    total = execution.cost_usd or Decimal("0")
    sources = [execution.cost_source or "unpriced"]
    tokens = execution.tokens_used or 0
    frontier = [execution.id]
    descendants = 0

    for _ in range(MAX_DELEGATION_DEPTH + 1):
        children = list(
            ExecutionLog.objects
            .filter(parent_step__execution_id__in=frontier)
            .values_list('id', 'cost_usd', 'cost_source', 'tokens_used')
        )
        if not children:
            break
        descendants += len(children)
        total += sum((c[1] or Decimal("0") for c in children), Decimal("0"))
        sources.extend(c[2] or "unpriced" for c in children)
        tokens += sum(c[3] or 0 for c in children)
        frontier = [c[0] for c in children]

    return {
        "delegated_run_count": descendants,
        "cost_usd_total": format_usd(total),
        "cost_source_total": combine_sources(sources),
        "tokens_used_total": tokens,
    }


def upsert_feedback(user, *, target: str, target_id, rating: int,
                    reason: str = '', comment: str = '') -> dict[str, Any] | None:
    """Upsert a thumbs up/down. Returns the feedback dict, or None on refusal.

    Every refusal is None (the view answers 404): a foreign target must not be
    distinguishable from a missing one.
    """
    from .models import Feedback

    if rating not in (1, -1):
        return {'error': 'rating must be +1 or -1'}
    if target == 'execution':
        from .models import ExecutionLog

        log = ExecutionLog.objects.filter(execution_id=target_id, user=user).first()
        if log is None:
            try:
                log = ExecutionLog.objects.filter(pk=target_id, user=user).first()
            except Exception:  # noqa: BLE001
                log = None
        if log is None:
            return None
        fb, _ = Feedback.objects.update_or_create(
            user=user, execution=log,
            defaults={'rating': rating, 'reason': reason or '',
                      'comment': (comment or '')[:2000]},
        )
    elif target == 'message':
        try:
            from chat.models import ChatMessage

            msg = ChatMessage.objects.filter(pk=target_id, session__user=user).first()
        except Exception:  # noqa: BLE001
            msg = None
        if msg is None:
            return None
        fb, _ = Feedback.objects.update_or_create(
            user=user, chat_message=msg,
            defaults={'rating': rating, 'reason': reason or '',
                      'comment': (comment or '')[:2000]},
        )
    else:
        return {'error': 'unknown target'}
    return {'rating': fb.rating, 'reason': fb.reason, 'comment': fb.comment}


def clear_feedback(user, *, target: str, target_id) -> bool:
    """Delete the caller's feedback. True when something was cleared."""
    from .models import Feedback

    if target == 'execution':
        qs = Feedback.objects.filter(user=user, execution__execution_id=target_id)
        if not qs.exists():
            try:
                qs = Feedback.objects.filter(user=user, execution_id=target_id)
            except Exception:  # noqa: BLE001
                pass
        deleted, _ = qs.delete()
        return bool(deleted)
    if target == 'message':
        deleted, _ = Feedback.objects.filter(
            user=user, chat_message_id=target_id).delete()
        return bool(deleted)
    return False


def feedback_for(user, execution=None, message_id=None) -> dict | None:
    """The caller's feedback on one target, or None."""
    from .models import Feedback

    if execution is not None:
        fb = Feedback.objects.filter(user=user, execution=execution).first()
    elif message_id is not None:
        fb = Feedback.objects.filter(user=user, chat_message_id=message_id).first()
    else:
        return None
    if fb is None:
        return None
    return {'rating': fb.rating, 'reason': fb.reason, 'comment': fb.comment}


def quality_summary(user, *, days: int = 30) -> dict[str, Any]:
    """Counts by failure category, thumbs, and signals. Excludes eval traffic."""
    from django.db.models import Count

    from .models import ExecutionLog, Feedback, RunSignal

    logs = ExecutionLog.objects.filter(
        user=user, created_at__gte=_since(days)).exclude(caller='eval')
    by_category = dict(
        logs.exclude(failure_category='').values('failure_category')
        .annotate(count=Count('id')).values_list('failure_category', 'count')
    )
    thumbs = Feedback.objects.filter(user=user, created_at__gte=_since(days))
    up = thumbs.filter(rating=1).count()
    down = thumbs.filter(rating=-1).count()
    by_reason = dict(
        thumbs.filter(rating=-1).exclude(reason='').values('reason')
        .annotate(count=Count('id')).values_list('reason', 'count')
    )
    signals = RunSignal.objects.filter(user=user, created_at__gte=_since(days))
    by_signal = dict(
        signals.values('kind').annotate(count=Count('id')).values_list('kind', 'count')
    )
    recent_down = list(
        thumbs.filter(rating=-1).select_related('execution').order_by('-created_at')[:20]
    )
    targets = []
    for fb in recent_down:
        if fb.execution_id:
            targets.append({
                'type': 'execution', 'id': str(fb.execution.execution_id),
                'agent': fb.execution.subagent.name if fb.execution.subagent else '',
                'reason': fb.reason, 'comment': (fb.comment or '')[:200],
            })
        elif fb.chat_message_id:
            targets.append({
                'type': 'message', 'id': fb.chat_message_id,
                'agent': '', 'reason': fb.reason,
                'comment': (fb.comment or '')[:200],
            })
    return {
        'days': days,
        'by_failure_category': by_category,
        'thumbs_up': up,
        'thumbs_down': down,
        'thumbs_down_by_reason': by_reason,
        'signals_by_kind': by_signal,
        'recent_thumbs_down': targets,
    }


def _delegated_by(execution: ExecutionLog) -> dict[str, Any] | None:
    """The orchestrator's side of a delegated run: who asked, and why.

    `parent_step.turn.reasoning` is the point of the whole `parent_step` FK —
    it is what the orchestrating agent was thinking at the moment it decided to
    hand this work off, which is the first thing you want when a worker did
    something surprising.
    """
    step = execution.parent_step
    if step is None:
        return None
    parent_run = step.execution
    return {
        "execution_id": str(parent_run.execution_id),
        "workflow_id": parent_run.subagent_id,
        "workflow_name": _agent_name(parent_run),
        "tool": step.tool,
        "call_id": step.call_id,
        "task": execution.delegation_task,
        "index": execution.delegation_index,
        "reasoning": step.turn.reasoning if step.turn else '',
        "turn_index": step.turn.index if step.turn else None,
    }


# ======================== Configuration history ========================

def _revision_summary(revision: SubAgentRevision | None) -> dict[str, Any] | None:
    if revision is None:
        return None
    return {
        "id": revision.id,
        "number": revision.number,
        "summary": revision.summary,
        "source": revision.source,
        "created_at": revision.created_at,
    }


def revision_timeline(
    user,
    agent_id: int,
    *,
    limit: int = REVISION_TIMELINE_LIMIT,
    cursor: str | None = None,
) -> dict[str, Any] | None:
    """One page of an agent's configuration changes, newest first, with diffs.

    Returns None when the agent does not exist or is not theirs — an id is not
    a permission.

    This used to answer with a single capped list and a `truncated` flag, which
    made a heavily-tuned agent's history unreadable: everything past the cap was
    simply unreachable, and the timeline was rendered inline in the builder
    where it grew without end. It is paged now, keyset on `number` — the column
    is monotonic per agent and already the sort key, so a save landing between
    two pages cannot make the next page repeat a row the way an offset would.
    `count` is on the first (uncursored) page only, matching the execution list.
    """
    from agents.models import SubAgent

    if not SubAgent.objects.filter(id=agent_id, user=user).exists():
        return None

    base = (
        SubAgentRevision.objects
        .filter(subagent_id=agent_id)
        .select_related('user')
        .annotate(run_count=Count('executions'))
    )
    limit = min(limit, REVISION_TIMELINE_LIMIT)
    page = paginate_keyset(base, limit=limit, cursor=cursor, sort_field='number')
    return {
        "results": [
            {
                "id": row.id,
                "number": row.number,
                "summary": row.summary,
                "source": row.source,
                "diff": row.diff,
                "changed_by": row.user.username if row.user else None,
                # How many runs executed under this revision — the number that
                # says whether a change has been exercised enough to judge.
                "run_count": row.run_count,
                "created_at": row.created_at,
            }
            for row in page.items
        ],
        "count": None if cursor else base.count(),
        "limit": limit,
        "next_cursor": page.next_cursor,
        "has_more": page.has_more,
    }


def revision_detail(user, agent_id: int, number: int) -> dict[str, Any] | None:
    """One revision's full configuration snapshot."""
    from agents.models import SubAgent

    if not SubAgent.objects.filter(id=agent_id, user=user).exists():
        return None

    revision = (
        SubAgentRevision.objects
        .filter(subagent_id=agent_id, number=number)
        .select_related('user')
        .first()
    )
    if revision is None:
        return None

    return {
        "id": revision.id,
        "number": revision.number,
        "summary": revision.summary,
        "source": revision.source,
        "diff": revision.diff,
        "config": revision.config,
        "changed_by": revision.user.username if revision.user else None,
        "created_at": revision.created_at,
        "run_count": ExecutionLog.objects.filter(revision=revision).count(),
    }
