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

import json
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
    # MCP tools are resolved per-user at runtime rather than named here, so the
    # grant unlocks the whole user-configured set. Their names are namespaced by
    # `mcp_integration.tool_provider`, which is what keeps them from colliding
    # with a built-in and slipping past the allow-list.
    'mcp': (),
}

#: Granted in the builder but with no implementation the runtime is willing to
#: serve. `shell` has no sandbox at all (AGENT_TEMPLATES.md §9.1) and the chat
#: file tools were deliberately removed — see chat/tests_rework.py's
#: `RemovedCapabilityTests`. Re-adding either through the back door of an agent
#: grant would undo that decision silently, so the runtime refuses and says so
#: rather than pretending the grant was honoured.
UNSERVED_GRANTS = frozenset({'shell', 'fileOps'})

#: Available whatever the grants say: no side effects, no egress, no reads of
#: anything the user owns.
ALWAYS_AVAILABLE = ('get_current_time',)

#: How much of the sandbox's stdout to hand back to the model. A runaway loop
#: printing megabytes would otherwise blow the context window in one tool call.
MAX_TOOL_OUTPUT_CHARS = 20_000

PYTHON_TOOL_DESCRIPTOR = {
    'type': 'function',
    'function': {
        'name': 'execute_python',
        'description': (
            'Run Python in a restricted sandbox. No network, no filesystem, no '
            'imports beyond the safe standard set. Assign your answer to a '
            'variable named `result`, or print it. Use this for calculation and '
            'data manipulation, not for reaching the outside world.'
        ),
        'parameters': {
            'type': 'object',
            'properties': {
                'code': {'type': 'string', 'description': 'The Python source to run.'},
            },
            'required': ['code'],
        },
    },
}


class AgentRunRefused(Exception):
    """The run was rejected before any model call — a guardrail said no."""


# ── Tools ────────────────────────────────────────────────────────────────────

async def _execute_python(args: dict[str, Any]) -> str:
    """Run code through the same sandbox the Code node uses.

    Reusing `executor.sandbox` rather than adding a second sandbox is deliberate:
    a second one would be a second thing to get wrong, and this one is already
    the audited path (docs/SANDBOX_EXECUTION.md).
    """
    from executor.sandbox.safe_execution import get_sandbox

    code = (args.get('code') or '').strip()
    if not code:
        return "Error: 'code' is required."

    # The sandbox blocks on a worker thread; keep the event loop free.
    outcome = await sync_to_async(get_sandbox().execute, thread_sensitive=False)(code)

    if not outcome.get('success'):
        detail = outcome.get('error') or 'Execution failed.'
        if outcome.get('stderr'):
            detail = f"{detail}\n{outcome['stderr']}"
        return f'Error: {detail}'[:MAX_TOOL_OUTPUT_CHARS]

    payload = {
        'result': _jsonable(outcome.get('result')),
        'stdout': (outcome.get('output') or '')[:MAX_TOOL_OUTPUT_CHARS],
    }
    return json.dumps(payload)[:MAX_TOOL_OUTPUT_CHARS]


def _jsonable(value: Any) -> Any:
    """Sandbox code can return anything; the transcript only carries JSON."""
    try:
        json.dumps(value)
        return value
    except (TypeError, ValueError):
        return repr(value)


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
        from chat.tools import ToolExecutor

        allowed = self.allowed_names
        descriptors = [
            t for t in ToolExecutor.AVAILABLE_TOOLS
            if t.get('function', {}).get('name') in allowed
        ]
        if self.grants.get('codeExecution'):
            descriptors.append(PYTHON_TOOL_DESCRIPTOR)

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

        if name == 'execute_python':
            if not self.grants.get('codeExecution'):
                return _denied(name, 'code execution')
            return await _execute_python(args)

        if name not in self.allowed_names:
            return _denied(name, name)

        from chat.tools import ToolExecutor
        return await ToolExecutor.execute(name, args, context)


def _denied(name: str, capability: str) -> str:
    return (
        f"Error: '{name}' is not available to this agent — {capability} was not "
        f"granted. Do not try it again; solve the task with the tools you have, "
        f"or say what you would need."
    )


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
        (agent.context or '').strip() or '(No brief was given. Ask what is wanted.)',
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
    return '\n'.join(parts)


# ── Guardrails checked before the first token ────────────────────────────────

@sync_to_async
def _spend_this_month(agent, user) -> int:
    from django.db.models import Sum
    from logs.models import ExecutionLog

    start = timezone.now().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    total = (
        ExecutionLog.objects
        .filter(user=user, workflow=agent, created_at__gte=start)
        .aggregate(total=Sum('credits_used'))['total']
    )
    return total or 0


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


@sync_to_async
def _open_log(agent, user, goal: str, trigger_type: str):
    from logs.models import ExecutionLog

    return ExecutionLog.objects.create(
        workflow=agent,
        user=user,
        status='running',
        trigger_type=trigger_type,
        input_data={'goal': goal},
        supervision_level=agent.supervision_level or '',
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


async def run_agent(agent, goal: str, *, user, sink=None,
                    thread_id: str | None = None,
                    trigger_type: str = 'manual') -> AgentRun:
    """Run `agent` against `goal` and record the run.

    `thread_id` keys the checkpointer, so passing the id of a paused run is how
    an approved run resumes — the same mechanism chat uses, reached through
    `chat.agent.approve_tool_call`.
    """
    from chat.agent import TurnContext, iteration_limit, run_turn
    from chat.events import null_sink

    await check_guardrails(agent, user)

    started = time.monotonic()
    log = await _open_log(agent, user, goal, trigger_type)
    thread_id = thread_id or f'agent-{agent.id}-{uuid.uuid4()}'

    toolbox = AgentToolbox.for_agent(agent, user.id)
    gathered = await _gather_context(agent, user)
    guards = agent.guardrails or {}
    autonomy = guards.get('autonomy', 'ask')

    turn = TurnContext(
        provider=agent.llm_provider or 'openrouter',
        model=agent.llm_model or '',
        system_message=build_system_prompt(agent, gathered),
        user_id=user.id,
        session_id=thread_id,
        # The chat agent widens its iteration budget for research-shaped work.
        # An agent run is that shape by definition — it was given a goal, not a
        # question — so it gets the same headroom.
        intent='research',
        user_text=goal,
        memory_enabled=False,
        max_iterations=iteration_limit('research'),
        sink=sink or null_sink,
        tool_source=toolbox.descriptors,
        tool_dispatch=toolbox.dispatch,
        sensitive_tools=sensitive_tools_for(autonomy, toolbox),
    )

    try:
        result = await run_turn(turn, prompt=goal, thread_id=thread_id)
    except Exception as exc:
        logger.exception('[AgentRuntime] Agent %s failed', agent.id)
        await _close_log(log, status='failed', result={}, tokens=0, error=str(exc))
        raise

    await _close_log(
        log,
        status='paused' if result.awaiting_approval else 'completed',
        result={'answer': result.answer, 'tool_trace': result.tool_trace},
        tokens=result.tokens,
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
    )
