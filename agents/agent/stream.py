"""
Record what an agent run did and thought, and announce it as it happens.

This module is the whole write path for agent observability. It sits on three
hooks the turn loop offers (`sink`, `on_model_turn`, `on_tool_result`) and turns
one run into rows in `logs/`:

    on_model_turn  -> AgentTurn   (one model call: its reasoning, its decision)
    on_tool_result -> AgentStep   (one tool call, attached to the turn above)

**Why the turn is recorded, not inferred.** An agent does not run a pipeline. It
reasons, issues zero or more calls *together*, gets every result back into the
same model, and reasons again. Calls sharing a turn were decided at the same
moment; calls in different turns were not. The `AgentTurn` row is what carries
that grouping and each turn's reasoning in full — queryable, and attributable
per turn rather than copied across every call in it.

**Why it is persisted as well as broadcast.** Broadcasting alone shows a live run
and loses it when the socket closes. The rows are what let a finished run be
reopened afterwards — they are what the `/api/logs/` endpoints read (see
`logs/queries.py`), and the only record of a run that outlives its socket.

**Ordering matters.** The turn row is written before its steps, and each step row
before its frame is broadcast. A client that reacts to a frame by fetching the
run must not find a trace missing the step it was just told about.

**Nothing here may fail a run.** Every write and every broadcast is wrapped. An
agent that did the work must not be reported as failed because something was
watching it.
"""
from __future__ import annotations

import logging
from typing import Any

from asgiref.sync import sync_to_async
from django.utils import timezone

from decimal import Decimal

from chat.turn.events import Event
from llm.pricing import combine_sources
from llm.usage import EMPTY_USAGE, TokenUsage
from workflow_backend.thresholds import (
    TURN_CONTENT_CHAR_LIMIT,
    TURN_REASONING_CHAR_LIMIT,
)

logger = logging.getLogger(__name__)

#: Cap on how much of a tool result is persisted or broadcast. Whole documents
#: come back from `scrape_webpage` and `knowledge_base_search`; the canvas shows
#: a preview and the model already has the full text in its transcript, so
#: storing it a second time buys nothing and bloats every log row.
MAX_PAYLOAD_CHARS = 4000

#: How much reasoning to put on the wire. The stored row keeps far more
#: (`TURN_REASONING_CHAR_LIMIT`); a live frame only has to be readable.
BROADCAST_REASONING_CHARS = 600


def _truncate(value: Any) -> Any:
    """Bound a payload for storage, marking it when something was dropped."""
    if isinstance(value, str) and len(value) > MAX_PAYLOAD_CHARS:
        return value[:MAX_PAYLOAD_CHARS] + f'… [truncated, {len(value)} chars total]'
    return value


def _clip(text: str, limit: int) -> tuple[str, bool]:
    """Bound one text field, reporting whether it was cut.

    The flag matters more than the cut: a trimmed thought and a genuinely brief
    one must not look alike to whoever is reading the run to work out what the
    agent was doing.
    """
    text = text or ''
    if len(text) <= limit:
        return text, False
    return text[:limit], True


class AgentRunStream:
    """Bridges one agent run onto the execution channel and the log tables.

    One instance per run. It is deliberately stateful: `_order` numbers the
    steps so a projection can rebuild the sequence without timestamps (which
    would tie step order to clock resolution), and `_turn_id` holds the turn
    every subsequent tool call belongs to.
    """

    def __init__(self, execution_log, *, broadcaster=None) -> None:
        self._log = execution_log
        self.execution_id = str(execution_log.execution_id)
        self._order = 0
        #: call_id -> the open `AgentStep` row id, so the result closes the row
        #: the start opened instead of writing a second one for the same call.
        self._pending: dict[str, int] = {}
        #: The turn currently issuing tool calls. None before the first model
        #: call, or if writing that turn's row failed — a step with no turn is
        #: still recorded, just unattributed (`queries.unattributed_steps`).
        self._turn_id: int | None = None
        #: What context curation did across the whole run, folded into
        #: `output_data` at close. A per-pass row would be a lot of noise
        #: for a number the user only ever reads as a total.
        self.curation: dict[str, Any] = {
            'passes': 0, 'results_compacted': 0, 'steps_folded': 0,
            'summary_tokens': 0, 'tokens_saved': 0, 'archived_ids': [],
            # What the folds themselves cost. The fold is a real model call and
            # is charged for like one — it counts against the run's tokens, so
            # it has to count against the run's money too, or the guardrail
            # curation exists to serve cannot see curation's own spend.
            'cost_usd': Decimal('0'), 'cost_source': '',
        }
        if broadcaster is None:
            from streaming.broadcaster import get_broadcaster

            broadcaster = get_broadcaster()
        self._broadcaster = broadcaster

    # ── the sink half: events the agent emits before a tool runs ─────────────

    async def sink(self, event: Event, payload: dict[str, Any]) -> None:
        """An `EventSink`. Must never raise — it is called from the tool loop."""
        try:
            if event == Event.AGENT_TRACE and payload.get('sub_type') == 'tool':
                await self._tool_started(payload)
            elif event == Event.ASK_PERMISSION:
                await self._approval_requested(payload)
            elif event == Event.ERROR:
                await self._run_failed(payload)
        except Exception:  # noqa: BLE001 — a broken stream must not fail the run
            logger.exception('[AgentStream] Failed to relay %s', event)

    async def _tool_started(self, payload: dict[str, Any]) -> None:
        """Open the step row now, before the tool actually runs.

        Two reasons it cannot wait for the result. A tool that delegates
        (`invoke_subagent`, `run_agent`) starts its worker runs *during*
        dispatch, and each of those records the step that asked for it — so the
        row has to exist by then or the delegation link is silently dropped.
        And a long tool call is otherwise invisible in the persisted trace until
        it finishes, which is exactly when someone is looking to find out what
        the agent is stuck on.
        """
        self._order += 1
        call_id = payload.get('call_id') or f'step-{self._order}'
        tool = payload.get('tool', 'tool')

        try:
            self._pending[call_id] = await self._open_step(
                call_id=call_id, tool=tool, order=self._order,
                args=payload.get('args') or {},
            )
        except Exception:  # noqa: BLE001
            logger.exception('[AgentStream] Failed to open step %s', call_id)

        await self._broadcaster.node_started(
            self.execution_id,
            node_id=call_id,
            node_type=tool,
            node_name=tool,
            input_data={'args': _truncate(payload.get('args'))},
        )

    @sync_to_async
    def _open_step(self, *, call_id: str, tool: str, order: int,
                   args: dict[str, Any]) -> int:
        from logs.models import AgentStep

        step = AgentStep.objects.create(
            execution=self._log,
            # The turn that issued this call. `execution` is kept alongside it
            # because the run is what every other subsystem keys by, and a step
            # whose turn write failed must still be reachable.
            turn_id=self._turn_id,
            call_id=call_id,
            tool=tool,
            status='running',
            order=order,
            args={'args': _truncate(args)},
            started_at=timezone.now(),
        )
        return step.id

    async def _approval_requested(self, payload: dict[str, Any]) -> None:
        """Surface a paused tool call on the channel the canvas already watches.

        Sent as a `hitl_request` so the existing approval UI applies unchanged;
        `call_id` rides along because that — not a fresh id — is what
        `chat.agent.approve_tool_call` needs to resume the run.

        The socket frame reaches whoever is watching *now*. The queue row
        written alongside it is what reaches someone who is not: it is what the
        Inbox lists, and what arms the reminder ladder that eventually emails a
        digest. Both are needed — a run that pauses at 02:00 has no watcher, and
        one whose tab is open should not have to wait for a sweep.
        """
        call_id = payload.get('call_id', '')
        tool = payload.get('tool', 'this tool')
        message = (
            f"The agent wants to call {tool}. "
            'It will not run until you approve it.'
        )

        from .hitl import open_request

        await open_request(
            self._log, call_id=call_id, tool=tool, message=message,
        )

        await self._broadcaster.hitl_request(
            self.execution_id,
            request_id=call_id,
            request_type='tool_approval',
            title=f'Approve {tool}?',
            message=message,
            options=[{'label': 'Approve', 'value': 'approve'},
                     {'label': 'Reject', 'value': 'reject'}],
        )

    async def _run_failed(self, payload: dict[str, Any]) -> None:
        await self._broadcaster.workflow_error(
            self.execution_id,
            error=str(payload.get('error') or payload.get('message') or 'Run failed'),
        )

    # ── the turn half: what the model thought, and what it decided ───────────

    async def on_model_turn(self, *, index: int, reasoning: str, content: str,
                            decision: str, provider: str, model_id: str,
                            tokens: int, duration_ms: int,
                            usage: TokenUsage | None = None) -> None:
        """A `TurnObserver`. Record the turn, then announce it.

        Recorded *before* its tool calls run, which is what gives every step
        that follows something to belong to — and what lets a client show why
        the calls are about to happen rather than inferring it afterwards.
        """
        try:
            self._turn_id = await self._record_turn(
                index=index, reasoning=reasoning, content=content,
                decision=decision, provider=provider, model_id=model_id,
                tokens=tokens, duration_ms=duration_ms,
                usage=usage if usage is not None else EMPTY_USAGE,
            )
        except Exception:  # noqa: BLE001
            # Steps from this turn become unattributed rather than lost.
            self._turn_id = None
            logger.exception('[AgentStream] Failed to persist turn %s', index)

        try:
            await self._broadcaster.agent_turn(
                self.execution_id,
                index=index,
                reasoning=(reasoning or '')[:BROADCAST_REASONING_CHARS],
                decision=decision,
                model_id=model_id,
                tokens=tokens,
            )
        except Exception:  # noqa: BLE001
            logger.exception('[AgentStream] Failed to broadcast turn %s', index)

    @sync_to_async
    def _record_turn(self, *, index: int, reasoning: str, content: str,
                     decision: str, provider: str, model_id: str,
                     tokens: int, duration_ms: int,
                     usage: TokenUsage = EMPTY_USAGE) -> int:
        from logs.models import AgentTurn
        from llm.pricing import cost_for_usage

        clipped_reasoning, reasoning_cut = _clip(reasoning, TURN_REASONING_CHAR_LIMIT)
        clipped_content, content_cut = _clip(content, TURN_CONTENT_CHAR_LIMIT)

        # Priced here, per turn, against `model_id` — not once per run against
        # a run-level total. A run can resolve a different model on a resume,
        # and there is no single rate that could be applied to a sum of turns
        # billed at different ones.
        cost, source = cost_for_usage(model_id or '', usage)

        # A resumed run re-enters the loop at a turn index it has already used,
        # so this updates rather than inserts — `(execution, index)` is unique,
        # and a second row for the same turn would double-count its tokens.
        turn, _ = AgentTurn.objects.update_or_create(
            execution=self._log,
            index=index,
            defaults={
                'reasoning': clipped_reasoning,
                'reasoning_truncated': reasoning_cut,
                'content': clipped_content,
                'content_truncated': content_cut,
                'decision': decision,
                'provider': provider or '',
                'model_id': model_id or '',
                'tokens': tokens,
                'input_tokens': usage.input,
                'output_tokens': usage.output,
                'cached_read_tokens': usage.cached_read,
                'cached_write_tokens': usage.cached_write,
                'reasoning_tokens': usage.reasoning,
                'cost_usd': cost,
                'cost_source': source,
                'duration_ms': duration_ms,
            },
        )
        return turn.id

    async def on_curation(self, *, results_compacted: int, steps_folded: int,
                          tokens_before: int, tokens_after: int,
                          summary_tokens: int, archived_ids: tuple = (),
                          summary_cost_usd=None,
                          summary_cost_source: str = '') -> None:
        """A curation observer. Announce the cut and keep a running total.

        Deliberately not an `AgentTurn` row: `(execution, index)` is unique on
        that table and a curation has no place in the model's turn numbering —
        inventing an index for it would either collide with a real turn or
        renumber the ones after it. The run-level totals are folded into
        `output_data` when the run closes, which is where a fact about the whole
        run belongs.

        Must never raise, for the same reason the other observers must not:
        watching a run may not break it.
        """
        self.curation['passes'] += 1
        self.curation['results_compacted'] += results_compacted
        self.curation['steps_folded'] += steps_folded
        self.curation['summary_tokens'] += summary_tokens
        self.curation['tokens_saved'] += max(tokens_before - tokens_after, 0)
        self.curation['archived_ids'].extend(archived_ids)
        if summary_cost_usd:
            self.curation['cost_usd'] += Decimal(str(summary_cost_usd))
        if summary_cost_source:
            self.curation['cost_source'] = combine_sources(
                [self.curation['cost_source'], summary_cost_source]
            )

        try:
            await self._broadcaster.context_curated(
                self.execution_id,
                results_compacted=results_compacted,
                steps_folded=steps_folded,
                tokens_before=tokens_before,
                tokens_after=tokens_after,
                summary_tokens=summary_tokens,
            )
        except Exception:  # noqa: BLE001
            logger.exception('[AgentStream] Failed to broadcast curation')

    # ── the observer half: what actually happened when the tool returned ─────

    async def on_tool_result(self, *, call_id: str, name: str, args: dict[str, Any],
                             output: Any, status: str, duration_ms: int,
                             iteration: int = 0, thought: str = '') -> None:
        """A `ToolObserver`. Persist the step, then announce it.

        `iteration` and `thought` are still accepted because the turn loop still
        sends them, but neither is stored any more: the turn number is the
        `turn` FK, and the reasoning is `AgentTurn.reasoning` in full rather
        than the 150-character slice `thought` carries.
        """
        step_id = self._pending.pop(call_id, None)
        error = str(output) if status == 'failed' else ''
        try:
            if step_id is None:
                # No matching start: the call bypassed the trace event. Record
                # it anyway — an unopened step is still a real step — and give
                # it the next order number rather than dropping it.
                self._order += 1
                await self._record_step(
                    call_id=call_id, tool=name, order=self._order, args=args,
                    output=output, status=status, duration_ms=duration_ms,
                    error=error,
                )
            else:
                await self._close_step(
                    step_id=step_id, output=output, status=status,
                    duration_ms=duration_ms, error=error,
                )
        except Exception:  # noqa: BLE001
            logger.exception('[AgentStream] Failed to persist step %s', call_id)

        try:
            await self._broadcaster.node_completed(
                self.execution_id,
                node_id=call_id,
                output_preview={'result': _truncate(output)},
                duration_ms=duration_ms,
                status=status,
            )
            if status == 'failed':
                await self._broadcaster.node_error(
                    self.execution_id, node_id=call_id, error=error,
                )
        except Exception:  # noqa: BLE001
            logger.exception('[AgentStream] Failed to broadcast step %s', call_id)

    @sync_to_async
    def _close_step(self, *, step_id: int, output: Any, status: str,
                    duration_ms: int, error: str) -> None:
        """Finish the row `_open_step` created, rather than writing a second."""
        from logs.models import AgentStep

        AgentStep.objects.filter(id=step_id).update(
            status=status,
            result={'result': _truncate(output)},
            duration_ms=duration_ms,
            error_message=error,
            completed_at=timezone.now(),
        )

    @sync_to_async
    def _record_step(self, *, call_id: str, tool: str, order: int,
                     args: dict[str, Any], output: Any, status: str,
                     duration_ms: int, error: str) -> None:
        """Write a finished step that was never opened. See `on_tool_result`."""
        from logs.models import AgentStep

        AgentStep.objects.create(
            execution=self._log,
            turn_id=self._turn_id,
            call_id=call_id,
            tool=tool,
            status=status,
            order=order,
            args={'args': _truncate(args)},
            result={'result': _truncate(output)},
            duration_ms=duration_ms,
            error_message=error,
            completed_at=timezone.now(),
        )

    # ── run lifecycle ────────────────────────────────────────────────────────

    async def run_queued(self) -> None:
        """The run is waiting for an admission slot. Best-effort like every
        frame here: a run must not fail because nobody could be told it is
        waiting, and `workflow_start` on admission is still the signal that
        it began executing."""
        await self._safe(self._broadcaster.workflow_queued(self.execution_id))

    async def run_started(self, goal: str) -> None:
        # A resumed run continues an execution that already has steps. Starting
        # the counter at zero again would give two rows the same `order`, and
        # the projection orders by it — the trace would come back interleaved.
        self._order = await self._last_order()
        await self._safe(self._broadcaster.workflow_started(
            self.execution_id,
            # Reads `subagent_id` since `Workflow` was dropped. The broadcast
            # key keeps its wire name because BrowserOS and the canvas ship
            # their own builds and parse `workflow_id`; what it identifies is
            # the agent, which is what it always identified for an agent run.
            workflow_id=self._log.subagent_id,
            workflow_name=goal[:120],
        ))

    @sync_to_async
    def _last_order(self) -> int:
        from django.db.models import Max

        from logs.models import AgentStep

        return AgentStep.objects.filter(execution=self._log).aggregate(
            top=Max('order')
        )['top'] or 0

    async def run_finished(self, *, status: str, answer: str,
                           duration_ms: int) -> None:
        # The last turn's decision is only knowable once the run ends: the model
        # that "answered" may in fact have paused for approval, or failed.
        if status in ('paused', 'failed'):
            await self._safe(self._mark_last_turn(
                'paused' if status == 'paused' else 'error'
            ))

        if status == 'failed':
            await self._safe(self._broadcaster.workflow_error(
                self.execution_id, error=answer,
            ))
        else:
            await self._safe(self._broadcaster.workflow_completed(
                self.execution_id,
                output={'answer': _truncate(answer), 'status': status},
                duration_ms=duration_ms,
            ))

    @sync_to_async
    def _mark_last_turn(self, decision: str) -> None:
        from logs.models import AgentTurn

        last = (
            AgentTurn.objects.filter(execution=self._log)
            .order_by('-index')
            .first()
        )
        if last is not None and last.decision != decision:
            last.decision = decision
            last.save(update_fields=['decision'])

    @staticmethod
    async def _safe(coro) -> None:
        try:
            await coro
        except Exception:  # noqa: BLE001
            logger.exception('[AgentStream] Broadcast failed')


def tee(*sinks):
    """Fan one agent's events out to several sinks.

    `run_agent` already accepts a caller's sink (SSE, tests). Replacing it with
    the canvas stream would silently break those callers, so both receive every
    event instead.
    """
    async def _fanout(event: Event, payload: dict[str, Any]) -> None:
        for sink in sinks:
            if sink is None:
                continue
            try:
                await sink(event, payload)
            except Exception:  # noqa: BLE001
                logger.exception('[AgentStream] Sink %r raised', sink)

    return _fanout
