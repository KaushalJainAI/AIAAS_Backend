"""
The agent runtime: what actually executes a saved agent.

Until this module existed an agent was a row — the builder saved a complete
`AgentConfig` and nothing could run it, so every `runs` count was zero. See
docs/AGENT_TEMPLATES.md §8, Phase 1.

**The one rule this module exists to keep.** The permissions screen an installer
sees is rendered from `tool_grants` / `guardrails` / `agent_context`. If the
runtime read anything else, or defaulted a missing grant to "allowed", the
screen would be a promise nothing enforces. So the grant map below is the only
place a tool becomes reachable, and `AgentToolbox.dispatch` re-checks the grant
at call time rather than trusting that an ungranted tool was merely never
advertised — a model that remembers a tool name from its training data will
happily call one it was not offered.

**Why it borrows the chat loop.** `chat.agent` already threads tool calls as a
proper transcript, streams to a sink, and pauses on `interrupt()` for approval.
Forking it would mean maintaining two loops that must agree on message
threading, which is exactly the thing the chat rewrite was about getting right.
Instead `TurnContext` takes three optional hooks — `tool_source`,
`tool_dispatch`, `sensitive_tools` — and this module supplies all three. Chat
turns pass none and behave exactly as before.
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass
from typing import Any

from asgiref.sync import sync_to_async
from django.utils import timezone

logger = logging.getLogger(__name__)

#: Grant key -> the built-in tool names it unlocks. This map *is* the
#: permissions model; a tool absent from every value is unreachable by an agent
#: no matter what the model asks for.
GRANT_TOOLS: dict[str, tuple[str, ...]] = {
    'webSearch': ('web_search', 'deep_research', 'image_search', 'video_search'),
    'scrape': ('scrape_webpage', 'read_url'),
    'rag': ('list_knowledge_bases', 'knowledge_base_search'),
    'codeExecution': ('execute_python',),
    # Delegation. There is no 'orchestrator' kind of agent — an agent that
    # fans out to other agents is one holding this grant, so composition is
    # checked by the same mechanism as every other capability instead of by a
    # second code path that could disagree with it.
    'subAgents': ('invoke_subagent', 'search_agents'),
    # MCP tools are resolved per-user at runtime rather than named here, so the
    # grant unlocks the whole user-configured set. Their names are namespaced by
    # `mcp_integration.tool_provider`, which is what keeps them from colliding
    # with a built-in and slipping past the allow-list.
    'mcp': (),
}

#: Granted in the builder but with no implementation the runtime is willing to
#: serve. `shell` has no sandbox at all (AGENT_TEMPLATES.md §9.1) and the chat
#: file tools were deliberately removed — see chat/tests/test_rework.py's
#: `RemovedCapabilityTests`. Re-adding either through the back door of an agent
#: grant would undo that decision silently, so the runtime refuses and says so
#: rather than pretending the grant was honoured.
UNSERVED_GRANTS = frozenset({'shell', 'fileOps'})

#: Available whatever the grants say: no side effects, no egress, no reads of
#: anything the user owns.
ALWAYS_AVAILABLE = ('get_current_time',)


class AgentRunRefused(Exception):
    """The run was rejected before any model call — a guardrail said no."""


# ── Tools ────────────────────────────────────────────────────────────────────
#
# Code execution (`execute_python`) is declared and implemented in the chat
# tool registry, where it runs the same `executor.sandbox` as every other
# caller. This module keeps only what is actually its own: the grant that
# decides whether an agent may reach it.


@dataclass(slots=True)
class AgentToolbox:
    """The tools one agent may see and use, derived from its grants alone."""

    grants: dict[str, bool]
    user_id: int
    #: Grants the builder set that this runtime will not serve. Surfaced so the
    #: caller can tell the user, instead of the agent silently lacking a tool
    #: its brief assumes it has.
    unserved: tuple[str, ...] = ()

    @classmethod
    def for_agent(cls, agent, user_id: int) -> AgentToolbox:
        grants = {k: bool(v) for k, v in (agent.tool_grants or {}).items()}
        unserved = tuple(sorted(g for g in UNSERVED_GRANTS if grants.get(g)))
        return cls(grants=grants, user_id=user_id, unserved=unserved)

    @property
    def allowed_names(self) -> frozenset[str]:
        names = set(ALWAYS_AVAILABLE)
        for grant, tools in GRANT_TOOLS.items():
            if self.grants.get(grant):
                names.update(tools)
        return frozenset(names)

    @property
    def mcp_allowed(self) -> bool:
        return bool(self.grants.get('mcp'))

    async def descriptors(self) -> list[dict[str, Any]]:
        """The tool list the model is offered this turn."""
        from chat.tools import AVAILABLE_TOOLS

        allowed = self.allowed_names
        descriptors = [
            t for t in AVAILABLE_TOOLS
            if t.get('function', {}).get('name') in allowed
        ]

        if self.mcp_allowed:
            try:
                from mcp_integration.tool_provider import MCPToolProvider
                descriptors.extend(
                    await MCPToolProvider.get_openai_tool_descriptors(self.user_id)
                )
            except Exception:
                # A dead MCP server must degrade the agent, not fail the run.
                logger.warning('[AgentRuntime] MCP tools unavailable for user %s',
                               self.user_id, exc_info=True)
        return descriptors

    async def dispatch(self, name: str, args: dict[str, Any],
                       context: dict[str, Any]) -> str:
        """Run a tool, refusing anything the grants do not cover.

        Checked here and not only at advertising time: the model can name a tool
        it was never offered, and "we didn't mention it" is not access control.
        """
        from mcp_integration.tool_provider import is_mcp_tool

        if is_mcp_tool(name):
            if not self.mcp_allowed:
                return _denied(name, 'MCP tools')
            from mcp_integration.tool_provider import MCPToolProvider
            return await MCPToolProvider.execute(name, args, self.user_id)

        if name == 'execute_python' and not self.grants.get('codeExecution'):
            return _denied(name, 'code execution')

        if name not in self.allowed_names:
            return _denied(name, name)

        from chat.tools import execute_tool
        return await execute_tool(name, args, context)


def _denied(name: str, capability: str) -> str:
    return (
        f"Error: '{name}' is not available to this agent — {capability} was not "
        f"granted. Do not try it again; solve the task with the tools you have, "
        f"or say what you would need."
    )


def approval_policy_for(autonomy: str):
    """Which per-call gate applies, for the calls `sensitive_tools_for` cannot name.

    MCP tool names are minted at runtime, so the name list above can never
    contain them — and an agent run is where that matters most, because nobody
    wrote the message that produced the call and nobody is watching the answer.
    `chat.permissions.unattended_policy` therefore gates every credentialed MCP
    call here, including reads that chat lets through on the strength of a human
    being present.

    `full` opts out entirely: the user said no interruptions, and a gate they
    did not ask for would be this module deciding it knows better than the
    setting it was given.
    """
    from chat.tools import permissions

    if autonomy == 'full':
        return permissions.never
    return permissions.unattended_policy


def sensitive_tools_for(autonomy: str, toolbox: AgentToolbox) -> frozenset[str]:
    """Which tool calls pause for a human, per the agent's autonomy setting.

    `review` pausing on *everything* is what the word has to mean — a review
    setting that quietly exempted some calls would be the permissions screen
    lying again, just in the other direction.
    """
    if autonomy == 'full':
        return frozenset()
    if autonomy == 'review':
        return toolbox.allowed_names | {'execute_python'}
    # 'ask': the calls with side effects outside our own walls.
    from chat.tools import SENSITIVE_TOOLS
    return frozenset(SENSITIVE_TOOLS) | {'execute_python'}


# ── Who is asking ────────────────────────────────────────────────────────────

#: Every way a run can begin. The runtime has exactly one entry point and these
#: are its callers; they differ in configuration, never in code path. A second
#: way to start a run is a second place for the guardrail checks to be
#: forgotten.
CALLERS = frozenset({'chat', 'orchestrator', 'trigger', 'api'})

#: Callers where no human is present at the moment the run starts. `chat` and
#: `api` are both a person pressing something; `orchestrator` inherits the
#: attendedness of whatever started *it*, and is treated as unattended because
#: the safe answer is the one that asks more often, not less.
UNATTENDED_CALLERS = frozenset({'trigger', 'orchestrator'})


class UnattendedNotPermitted(AgentRunRefused):
    """A trigger tried to run an agent that was never cleared to run alone."""


def _check_unattended(agent, caller: str) -> None:
    """A run with nobody watching has to have been asked for, once, explicitly.

    `allow_unattended` defaults to False and no migrated row sets it. That is
    deliberate: a trigger is a way for something other than the user — a clock,
    or an inbound HTTP request — to spend their model credits, and inheriting
    that permission from "the agent exists" is how a webhook becomes a bill.
    """
    if caller in UNATTENDED_CALLERS and not agent.allow_unattended:
        raise UnattendedNotPermitted(
            f'"{agent.name}" is not enabled for unattended runs. Turn on '
            f'"allow unattended" in its settings if you want a trigger or '
            f'another agent to be able to run it.'
        )


# ── The brief ────────────────────────────────────────────────────────────────

@sync_to_async
def _gather_context(agent, user) -> dict[str, Any]:
    """Read the rows the agent's brief refers to, re-scoped to the owner.

    Re-filtering on `user` here rather than trusting the stored ids is the same
    guard `agents.py` applies on write: ownership can be revoked between saving
    an agent and running it.
    """
    from inference.models import KnowledgeBase
    from skills.models import Skill

    ctx = agent.agent_context or {}
    skills = list(
        Skill.objects.filter(user=user, id__in=ctx.get('skills') or [])
        .values_list('title', 'content')
    )
    kbs = list(
        KnowledgeBase.objects.filter(user=user, id__in=ctx.get('knowledgeBases') or [])
        .values_list('name', flat=True)
    )
    return {'skills': skills, 'knowledge_bases': kbs, 'ctx': ctx}


def build_system_prompt(agent, gathered: dict[str, Any]) -> str:
    """Assemble the agent's standing instructions.

    Guardrails are stated to the model as well as enforced in code. Enforcement
    is what makes them true; telling the model is what stops it burning turns
    planning around a tool it will never be handed.
    """
    guards = agent.guardrails or {}
    grants = agent.tool_grants or {}
    parts: list[str] = [
        f'You are {agent.name}, an autonomous agent.',
        '',
        'YOUR BRIEF',
        (agent.prompt or '').strip() or '(No brief was given. Ask what is wanted.)',
    ]

    if gathered['skills']:
        parts += ['', 'SKILLS — instructions you have been given for this work:']
        for title, content in gathered['skills']:
            parts.append(f'\n## {title}\n{content}')

    if gathered['knowledge_bases']:
        names = ', '.join(gathered['knowledge_bases'])
        parts += ['', f'KNOWLEDGE BASES you can search: {names}.']

    if gathered['ctx'].get('useEnvironment'):
        parts += ['', f'The current time is {timezone.now().isoformat()}.']

    granted = sorted(k for k, v in grants.items() if v and k not in UNSERVED_GRANTS)
    parts += [
        '',
        'LIMITS',
        f"- Capabilities granted: {', '.join(granted) or 'none beyond answering directly'}.",
        '- Any other tool will be refused. Do not retry a refused tool.',
    ]
    if guards.get('autonomy') != 'full':
        parts.append('- Some actions pause for human approval before they run.')
    if guards.get('egress', 'none') == 'none':
        parts.append('- Your sandbox has no network access.')
    unserved = sorted(g for g in UNSERVED_GRANTS if grants.get(g))
    if unserved:
        parts.append(
            f"- {', '.join(unserved)} was configured but is not available in this "
            f'environment. Work without it and say so if it blocks you.'
        )
    # An output contract is what lets a *configured* agent stand in for a
    # hardcoded tool: the UI renders the shape, not the words. See
    # agents/contracts.py.
    from agents import contracts

    return '\n'.join(parts) + contracts.instruction_for(
        contracts.resolve(getattr(agent, 'output_schema', None))
    )


# ── Guardrails checked before the first token ────────────────────────────────

@sync_to_async
def _spend_this_month(agent, user) -> int:
    """What this agent has cost its owner so far this month, in rupees.

    Derived from `tokens_used`, not `credits_used`: the latter is a column
    nothing writes, so summing it returned zero on every run and the spend cap
    below could never refuse anything however low it was set. The conversion
    lives in `agents.spend` because `views/agents.py` has to show the user the
    same number this refuses them on.
    """
    from django.db.models import Sum
    from logs.models import ExecutionLog

    from agents.spend import rupees_for

    start = timezone.now().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    tokens = (
        ExecutionLog.objects
        .filter(user=user, subagent=agent, created_at__gte=start)
        .aggregate(total=Sum('tokens_used'))['total']
    )
    return rupees_for(tokens)


async def check_guardrails(agent, user) -> None:
    """Refuse the run if a guardrail already rules it out.

    The spend cap is checked before the run, not after: a cap enforced only on
    completion has already let the money go.
    """
    cap = (agent.guardrails or {}).get('spendCapRupees')
    if cap:
        spent = await _spend_this_month(agent, user)
        if spent >= cap:
            raise AgentRunRefused(
                f'This agent has reached its monthly spend cap ({spent}/{cap}). '
                f'Raise the cap in its settings to keep running it.'
            )


# ── Run ──────────────────────────────────────────────────────────────────────

@dataclass(slots=True)
class AgentRun:
    """What one agent run produced."""

    execution_id: str
    answer: str
    thinking: str
    tool_trace: list[dict[str, Any]]
    tokens: int
    awaiting_approval: bool
    unserved_grants: tuple[str, ...]
    duration_ms: int
    #: Present when the agent was configured with an output contract and met
    #: it. None means either no contract or a contract it failed.
    structured: dict[str, Any] | None = None
    contract_error: str = ''


@sync_to_async
def _open_log(agent, user, goal: str, trigger_type: str, thread_id: str = '',
              *, caller: str = 'api', depth: int = 0,
              parent_step_id: int | None = None, delegation_task: str = '',
              delegation_index: int = 0):
    from logs.models import ExecutionLog
    from logs import revisions

    # The configuration this run will execute under, pinned at open time rather
    # than read back at close: an agent edited mid-run must not retroactively
    # change what its running executions claim to have used. `current()` mints
    # revision 1 for agents that predate revision tracking, so every run has
    # something to point at.
    revision = revisions.current(agent) if agent is not None else None

    return ExecutionLog.objects.create(
        subagent=agent,
        revision=revision,
        user=user,
        status='running',
        trigger_type=trigger_type,
        # `trigger_type` says how the run was invoked; `caller` says what
        # invoked it. Both a chat delegation and a direct API call arrive as
        # `trigger_type='api'`, and telling them apart is the difference
        # between "the user asked for this" and "an agent spent their credits".
        caller=caller,
        depth=depth,
        parent_step_id=parent_step_id,
        delegation_task=delegation_task,
        delegation_index=delegation_index,
        # `thread_id` is stored, not just the goal: it is the checkpointer key,
        # and resuming an approved run has to find the *same* log so the trace
        # stays one execution instead of splitting into two the canvas cannot
        # join back up.
        input_data={'goal': goal, 'thread_id': thread_id},
        started_at=timezone.now(),
    )


@sync_to_async
def _close_log(log, *, status: str, result: dict[str, Any], tokens: int,
               error: str = '') -> None:
    log.status = status
    log.output_data = result
    log.tokens_used = tokens
    log.error_message = error
    log.completed_at = timezone.now()
    if log.started_at:
        log.duration_ms = int(
            (log.completed_at - log.started_at).total_seconds() * 1000
        )
    log.save(update_fields=[
        'status', 'output_data', 'tokens_used', 'error_message',
        'completed_at', 'duration_ms', 'updated_at',
    ])


@sync_to_async
def _cancel_pending_hitl(log) -> int:
    """Withdraw the approvals a cancelled run will now never act on.

    A `HITLRequest` left `pending` after its run is gone is not merely stale:
    `notifications/reminders.py` sweeps on `status='pending'`, so it keeps
    escalating and lands in the daily digest, asking the user to approve a step
    that no longer exists.
    """
    from agents.models import HITLRequest

    return HITLRequest.objects.filter(execution=log, status='pending').update(
        status='cancelled', responded_at=timezone.now(),
    )


async def _finalise_cancelled(log, stream, started: float) -> None:
    """Close out a run whose task was cancelled.

    Shielded because this runs *inside* the `CancelledError` handler: a bare
    await here is itself a cancellation point, and the first ORM round-trip
    would re-raise before the log was ever written — which is the exact bug
    this function exists to fix. `chat/turn/runs.py::stop` solves the same
    problem the other way round, by finalising from the canceller rather than
    the cancellee; a run can also be cancelled by shutdown or by a parent task,
    where there is no canceller to do it, so this path has to stand alone.
    """
    async def _work() -> None:
        await _cancel_pending_hitl(log)
        await _close_log(log, status='cancelled', result={}, tokens=0,
                         error='Run cancelled.')
        await stream.run_finished(
            status='cancelled', answer='Run cancelled.',
            duration_ms=int((time.monotonic() - started) * 1000),
        )

    try:
        await asyncio.shield(_work())
    except asyncio.CancelledError:
        # Shield lets `_work` finish even though the await was interrupted.
        raise
    except Exception:  # noqa: BLE001
        logger.exception('[AgentRuntime] Could not finalise cancelled run')


async def run_agent(agent, goal: str, *, user, sink=None,
                    thread_id: str | None = None,
                    trigger_type: str = 'manual', caller: str = 'api',
                    depth: int = 0, log=None,
                    parent_step_id: int | None = None,
                    delegation_task: str = '',
                    delegation_index: int = 0) -> AgentRun:
    """Run `agent` against `goal` and record the run.

    `thread_id` keys the checkpointer, so passing the id of a paused run is how
    an approved run resumes — the same mechanism chat uses, reached through
    `chat.agent.approve_tool_call`.

    `log` lets a caller open the `ExecutionLog` first and hand it in, which is
    how `start_agent_run` can return an execution id before the run has done
    anything. Left None, this opens its own.
    """
    from llm import access as llm
    from chat.turn.agent import TurnContext, iteration_limit, run_turn
    from chat.turn.events import null_sink

    from .stream import AgentRunStream, tee

    # Checked here even when `start_agent_run` already checked it. The cost is
    # one aggregate query; the alternative is a flag that skips a spend limit,
    # and a safety check with an off switch is not a safety check. The same
    # reasoning covers the unattended gate: schedules and triggers reach this
    # function without passing through any view.
    _check_unattended(agent, caller)
    await check_guardrails(agent, user)

    started = time.monotonic()
    thread_id = thread_id or f'agent-{agent.id}-{uuid.uuid4()}'
    if log is None:
        log = await _open_log(
            agent, user, goal, trigger_type, thread_id,
            caller=caller, depth=depth, parent_step_id=parent_step_id,
            delegation_task=delegation_task, delegation_index=delegation_index,
        )

    # Every run streams to the execution channel, whether or not the caller
    # asked for a sink: that is what makes the run visible on the workflow
    # canvas, and a run nobody watched still has to be replayable afterwards.
    stream = AgentRunStream(log)
    await stream.run_started(goal)

    provider = agent.llm_provider or 'openrouter'
    model = agent.llm_model or ''

    try:
        # Repeated from `start_agent_run` for the callers that arrive here
        # directly — a schedule, a trigger, a resumed run. It costs one
        # credential lookup, and it is what turns "the agent failed" into "this
        # provider has no credential", which is the only version of the message
        # the user can act on. Inside the try so it closes the log and tells the
        # channel by the same path every other failure takes.
        await llm.preflight(provider=provider, model=model, user_id=user.id)

        toolbox = AgentToolbox.for_agent(agent, user.id)
        gathered = await _gather_context(agent, user)
        guards = agent.guardrails or {}
        autonomy = guards.get('autonomy', 'ask')

        turn = TurnContext(
            provider=provider,
            model=model,
            system_message=build_system_prompt(agent, gathered),
            user_id=user.id,
            session_id=thread_id,
            # The chat agent widens its iteration budget for research-shaped
            # work. An agent run is that shape by definition — it was given a
            # goal, not a question — so it gets the same headroom.
            intent='research',
            user_text=goal,
            memory_enabled=False,
            # The builder's slider, finally connected. Default 0.2 matches what
            # `AgentSerializer` has always declared and shown the user; until
            # `TurnContext.temperature` existed the value was stored and never
            # read, so every agent ran at the library default of 0.7 instead.
            temperature=float((agent.runtime_settings or {}).get('temperature', 0.2)),
            max_iterations=iteration_limit('research'),
            sink=tee(sink or null_sink, stream.sink),
            tool_source=toolbox.descriptors,
            tool_dispatch=toolbox.dispatch,
            sensitive_tools=sensitive_tools_for(autonomy, toolbox),
            approval_policy=approval_policy_for(autonomy),
            on_tool_result=stream.on_tool_result,
            # One `AgentTurn` row per model call. Without it every tool call in
            # the run is unattributed, and the reasoning behind the run is lost
            # when the process ends.
            on_model_turn=stream.on_model_turn,
            # A worker is one level deeper than whoever asked for it, and the
            # counter is what stops delegation multiplying without bound.
            depth=depth,
        )

        result = await run_turn(turn, prompt=goal, thread_id=thread_id)
    except asyncio.CancelledError:
        # Not reachable from `except Exception`: since 3.8 `CancelledError` is a
        # `BaseException`. Without this branch a cancelled run left its
        # `ExecutionLog` at `running` for ever, its HITL rows nudging the user
        # about a step that no longer existed, and the canvas showing a spinner
        # with nothing behind it.
        logger.info('[AgentRuntime] Agent %s cancelled', agent.id)
        await _finalise_cancelled(log, stream, started)
        raise
    except Exception as exc:
        logger.exception('[AgentRuntime] Agent %s failed', agent.id)
        await _close_log(log, status='failed', result={}, tokens=0, error=str(exc))
        await stream.run_finished(
            status='failed', answer=str(exc),
            duration_ms=int((time.monotonic() - started) * 1000),
        )
        raise

    # A steer posted just as the run finished has nobody left to read it;
    # dropping the slot keeps the mailbox from accumulating one entry per run.
    from chat.turn import steering
    if not result.awaiting_approval:
        steering.discard(thread_id)

    status = 'paused' if result.awaiting_approval else 'completed'
    structured, contract_error = _apply_contract(agent, result)
    payload: dict[str, Any] = {
        'answer': result.answer, 'tool_trace': result.tool_trace,
    }
    if structured is not None:
        payload['structured'] = structured
    if contract_error:
        payload['contract_error'] = contract_error

    await _close_log(
        log,
        status=status,
        result=payload,
        tokens=result.tokens,
        error=contract_error,
    )
    await stream.run_finished(
        status=status, answer=result.answer,
        duration_ms=int((time.monotonic() - started) * 1000),
    )

    return AgentRun(
        execution_id=str(log.execution_id),
        answer=result.answer,
        thinking=result.thinking,
        tool_trace=result.tool_trace,
        tokens=result.tokens,
        awaiting_approval=result.awaiting_approval,
        unserved_grants=toolbox.unserved,
        duration_ms=int((time.monotonic() - started) * 1000),
        structured=structured,
        contract_error=contract_error,
    )


def _apply_contract(agent, result) -> tuple[dict[str, Any] | None, str]:
    """Check a finished run against the contract it was configured with.

    Returns `(structured, error)`. A contract failure is *reported*, not
    repaired: an agent set up to produce research output and returning prose
    has failed at the thing it was configured for, and quietly wrapping the
    prose would make every agent appear to satisfy every contract — which
    would make the whole mechanism decorative.
    """
    from agents import contracts

    contract = contracts.resolve(getattr(agent, 'output_schema', None))
    if contract is None or result.awaiting_approval:
        return None, ''

    try:
        return contracts.coerce(result.answer, contract), ''
    except contracts.ContractError as exc:
        logger.warning('[AgentRuntime] Agent %s broke its output contract: %s',
                       agent.id, exc)
        return None, str(exc)


async def start_agent_run(agent, goal: str, *, user,
                          thread_id: str | None = None,
                          trigger_type: str = 'manual',
                          caller: str = 'api',
                          parent_step_id: int | None = None,
                          delegation_task: str = '',
                          delegation_index: int = 0) -> str:
    """Begin a run in the background and return its execution id immediately.

    Two things have to happen before the caller gets a response, and both are
    the reason this exists rather than the view spawning `run_agent` directly:

    - **Guardrails and credentials are checked first.** A run that cannot be
      paid for — a spent cap, or a provider this user has no key for — is
      refused while the caller is still waiting, so it surfaces as a 402 rather
      than as a run that appears to start and dies silently in the background.
    - **The `ExecutionLog` is opened first**, so the returned execution id is
      already subscribable. A client that opens the socket the moment it gets
      the id must not miss the first step.

    Uses `spawn()` rather than `asyncio.create_task`: the run outlives its
    request, and a detached task inheriting the request's executor would raise
    `CurrentThreadExecutor already quit` on its first ORM call — see
    `workflow_backend/background.py`.
    """
    from llm import access as llm
    from workflow_backend.background import spawn

    _check_unattended(agent, caller)
    await check_guardrails(agent, user)
    # A missing credential is the same class of problem as a spent budget: the
    # caller can fix it, but only if they are told. Raised here, before the log
    # exists, it reaches the view as an error naming the provider — instead of
    # a 202 followed by a run that dies on its first model call, where the only
    # trace is a failed execution the user has to go and open.
    await llm.preflight(
        provider=agent.llm_provider or 'openrouter',
        model=agent.llm_model or '',
        user_id=user.id,
    )
    thread_id = thread_id or f'agent-{agent.id}-{uuid.uuid4()}'
    log = await _open_log(
        agent, user, goal, trigger_type, thread_id,
        caller=caller, parent_step_id=parent_step_id,
        delegation_task=delegation_task, delegation_index=delegation_index,
    )

    async def _run() -> None:
        try:
            await run_agent(agent, goal, user=user, thread_id=thread_id,
                            trigger_type=trigger_type, caller=caller, log=log)
        except Exception:
            # `run_agent` already closed the log as failed and told the
            # channel. Nothing is waiting on this task, so swallow rather than
            # leave an unretrieved exception on the loop.
            logger.exception('[AgentRuntime] Background run of agent %s failed',
                             agent.id)

    spawn(_run(), name=f'agent-run:{log.execution_id}')
    return str(log.execution_id)


async def resume_agent_run(agent, *, user, thread_id: str) -> str | None:
    """Continue a run that paused for approval, on its original execution id.

    Approval on its own only records consent in the checkpoint — the paused run
    has already returned, so without this nothing would ever pick it back up
    and the user would approve into silence.

    Reuses the paused `ExecutionLog` rather than opening a new one: the canvas
    subscribes per execution, and a resumed half arriving on a second id would
    leave the trace split across two runs with no way to join them.
    """
    from workflow_backend.background import spawn

    log = await _find_paused_log(agent, thread_id)
    if log is None:
        logger.warning('[AgentRuntime] No paused run for thread %s', thread_id)
        return None

    goal = (log.input_data or {}).get('goal', '')
    await _reopen_log(log)

    async def _run() -> None:
        try:
            # `depth` is carried over from the log, not left to default to 0.
            # A worker that paused for approval would otherwise resume as
            # though the user had started it, and could delegate again past
            # MAX_DELEGATION_DEPTH — the counter is only a bound if it survives
            # a pause. `caller` stays 'api' on purpose: a human just approved
            # this, so the run is attended however it began, and re-deriving
            # `caller='trigger'` here would send it back through the unattended
            # gate that the pause has already made moot.
            await run_agent(agent, goal, user=user, thread_id=thread_id,
                            trigger_type=log.trigger_type or 'manual', log=log,
                            depth=log.depth or 0)
        except Exception:
            logger.exception('[AgentRuntime] Resume of agent %s failed', agent.id)

    spawn(_run(), name=f'agent-resume:{log.execution_id}')
    return str(log.execution_id)


@sync_to_async
def _find_paused_log(agent, thread_id: str):
    from logs.models import ExecutionLog

    return ExecutionLog.objects.filter(
        subagent=agent, status='paused', input_data__thread_id=thread_id,
    ).order_by('-started_at').first()


@sync_to_async
def _reopen_log(log) -> None:
    log.status = 'running'
    log.completed_at = None
    log.save(update_fields=['status', 'completed_at', 'updated_at'])
