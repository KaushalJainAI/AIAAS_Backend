"""
Tools that find and run the user's saved agents.

`run_agent` hands work to something the user is not watching, which is why it
is in `SENSITIVE_TOOLS` while `execute_python` is not.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re

from typing import Any, Dict, List

from django.core.exceptions import ValidationError

from .registry import tool

from tools_config.overlay import alimit

from tools_config.settings_schema import (
    _AGENT_ANSWER_CHARS,
    _AGENT_SEARCH_DEFAULT,
    _AGENT_SEARCH_MAX,
)

logger = logging.getLogger(__name__)

async def _parent_step_id(context: Dict) -> int | None:
    """The `AgentStep` row for the tool call currently running, if there is one.

    Only an *agent* run records steps, so this is None in plain chat — a chat
    turn has no `ExecutionLog` for a step to hang off. Returns None rather than
    raising for the same reason the observers swallow: provenance is worth
    having and never worth failing a delegation over.
    """
    from asgiref.sync import sync_to_async

    call_id = context.get("call_id")
    session_id = context.get("session_id")
    if not call_id or not session_id:
        return None

    from logs.models import AgentStep

    @sync_to_async
    def _lookup() -> int | None:
        return (
            AgentStep.objects
            # Indexed column rather than the JSON path: this resolves once
            # per delegation, inside the call the user is waiting on.
            .filter(call_id=call_id, execution__thread_id=session_id)
            .order_by('-id')
            .values_list('id', flat=True)
            .first()
        )

    try:
        return await _lookup()
    except Exception:  # noqa: BLE001
        logger.exception("Could not resolve the parent step for %s", call_id)
        return None


AGENT_SEARCH_DEFAULT_LIMIT = 10
AGENT_SEARCH_MAX_LIMIT = 25

#: Waiting is bounded well under `agent.TOOL_CALL_TIMEOUT` (120s): the tool has
#: to return a usable execution_id *before* the loop times it out, or the run
#: becomes unreachable — still going, with nothing holding its id.
AGENT_RUN_DEFAULT_WAIT = 60
AGENT_RUN_MAX_WAIT = 90
AGENT_RUN_POLL_SECONDS = 2

#: How much of a finished run's answer to hand back. Shares a value with
#: `sandbox.MAX_CODE_OUTPUT_CHARS` and nothing else: that one bounds sandbox
#: stdout, this one bounds another agent's reply. They were one constant while
#: both tools lived in the same file, which read as a rule about code output
#: being silently applied to prose.
AGENT_ANSWER_CHAR_LIMIT = 20_000

#: Statuses past which polling a run is pointless.
_TERMINAL_RUN_STATUSES = {'cancelled', 'failed', 'completed', 'timeout'}

#: The manager's tool for answering a worker that stopped to ask.
SUBAGENT_ANSWER_TOOL = "answer_subagent"

#: What a manager is told when a worker stops. The human is the boss, the
#: orchestrator the manager, the subagents the workhorses: a worker's request
#: comes to the manager, who answers it here — and only letting a worker *act*
#: can need the boss.
PAUSED_MESSAGE = (
    "The agent stopped to ask something — see `waiting_on`. Answer each item "
    "with answer_subagent (execution_id + call_id). For a question, give the "
    "answer if you know it from the conversation; if only the user knows, ask "
    "them with ask_user first, then pass their answer on. For an approval, "
    "decide approve or reject; when the user must decide, they are shown a "
    "card and their answer is used. Do not start the agent again."
)


async def pending_requests(execution_ids: list[str]) -> list[dict[str, Any]]:
    """What paused runs are waiting on, in a form a manager can act on.

    Read from the `HITLRequest` rows the runs opened when they paused, which
    already carry the rendered sentence and, for a question, its spec — so a
    manager sees the worker's request exactly as the Inbox would show it.
    """
    from asgiref.sync import sync_to_async
    from agents.models import HITLRequest

    ids = [str(e) for e in execution_ids if e]
    if not ids:
        return []

    @sync_to_async
    def _read() -> list[dict[str, Any]]:
        out = []
        for row in (HITLRequest.objects
                    .filter(execution__execution_id__in=ids, status='pending')
                    .select_related('execution__subagent')
                    .order_by('created_at')[:20]):
            ctx = row.context_data or {}
            detail = ctx.get('detail') or {}
            item = {
                "execution_id": str(row.execution.execution_id),
                "call_id": row.node_id,
                "agent": getattr(row.execution.subagent, 'name', '') or '',
                "kind": 'question' if row.request_type == 'clarification' else 'approval',
            }
            if item["kind"] == 'question':
                item["question"] = ctx.get('question') or {}
            else:
                item["tool"] = ctx.get('tool', '')
                item["request"] = detail.get('sentence') or row.message
            out.append(item)
        return out

    try:
        return await _read()
    except Exception:  # noqa: BLE001 — a status report must not fail the call
        logger.exception("Could not read what %s is waiting on", ids)
        return []


async def _pending_row(execution_id: str, call_id: str, user_id: Any):
    """The open request `answer_subagent` names, owned by this user, or None."""
    from agents.models import HITLRequest

    if not execution_id or not call_id or not user_id:
        return None
    try:
        return await (HITLRequest.objects
                      .filter(execution__execution_id=execution_id, node_id=call_id,
                              user_id=user_id, status='pending')
                      .select_related('execution__subagent').afirst())
    except (ValueError, ValidationError):
        return None


async def subagent_answer_needs_user(args: Dict, context: Dict) -> bool:
    """Whether a manager's `answer_subagent` call may need the person.

    Answering a worker's question, or refusing its request, is the manager's
    own call in every mode — a manager may always say no, and a question is not
    a side effect. Only *letting a worker act* can need the boss: that returns
    True here and then meets the usual gates (Ask mode shows the card; Auto
    lets the manager decide, under the same floor Auto keeps for its own calls
    — `reviewer.subagent_floor`). An unknown request needs nobody: the tool
    answers with the error.
    """
    if str((args or {}).get("decision") or "").strip().lower() == "reject":
        return False
    row = await _pending_row(str((args or {}).get("execution_id") or ""),
                             str((args or {}).get("call_id") or ""),
                             (context or {}).get("user_id"))
    return row is not None and row.request_type != 'clarification'


async def _await_agent_run(execution_id: str, wait_seconds: int,
                           answer_chars: int = _AGENT_ANSWER_CHARS) -> Dict[str, Any]:
    """Poll a run to a terminal state, or give up and report it still going."""
    from asgiref.sync import sync_to_async
    from logs.models import ExecutionLog

    @sync_to_async
    def _read() -> Dict[str, Any] | None:
        return (
            ExecutionLog.objects
            .filter(execution_id=execution_id)
            .values("status", "output_data", "error_message", "tokens_used")
            .first()
        )

    deadline = asyncio.get_running_loop().time() + wait_seconds
    row = await _read()
    while row is not None and row["status"] not in _TERMINAL_RUN_STATUSES:
        if row["status"] == "paused":
            return {
                "status": "paused",
                "waiting_on": await pending_requests([execution_id]),
                "message": PAUSED_MESSAGE,
            }
        if asyncio.get_running_loop().time() >= deadline:
            return {
                "status": "running",
                "message": (
                    f"Still running after {wait_seconds}s. Tell the user it is "
                    f"working and call get_agent_run with this execution_id later "
                    f"— do not start the agent again."
                ),
            }
        await asyncio.sleep(AGENT_RUN_POLL_SECONDS)
        row = await _read()

    if row is None:
        return {"status": "unknown", "message": "No run found for that execution_id."}

    output = row.get("output_data") or {}
    return {
        "status": row["status"],
        "answer": (output.get("answer") or "")[:answer_chars],
        "error": row.get("error_message") or "",
        "tokens_used": row.get("tokens_used") or 0,
    }


#: Description text is capped in the summary: the model is choosing between
#: agents here, not reading one.
_AGENT_BLURB_CHARS = 500


def _agent_tags(row: dict) -> list:
    """The row's tags, defended against a JSONField holding a non-list."""
    tags = row.get("tags") or []
    return tags if isinstance(tags, list) else []


def _agent_haystack(row: dict) -> str:
    """The text `search_agents` matches query terms against."""
    return " ".join([
        row.get("name") or "",
        row.get("description") or "",
        " ".join(str(t) for t in _agent_tags(row)),
    ]).lower()


def _agent_summary(row: dict) -> dict:
    """One agent as the model sees it in a search result.

    Deliberately not the full record: enough to choose between agents and to
    name one in `invoke_subagent`, and nothing that would cost a caller
    context it did not ask for.
    """
    grants = row.get("tool_grants") or {}
    return {
        "agent_id": row["id"],
        "name": row["name"],
        "description": (row.get("description") or "")[:_AGENT_BLURB_CHARS],
        "tags": _agent_tags(row),
        "status": row.get("status"),
        "granted_tools": sorted(k for k, v in grants.items() if v),
        "autonomy": (row.get("guardrails") or {}).get("autonomy", "ask"),
        "runs": row.get("execution_count") or 0,
        "last_run": (
            row["last_executed_at"].isoformat()
            if row.get("last_executed_at") else None
        ),
    }


def _agent_search_limit(args: Dict, max_results: int) -> int:
    """The caller's `limit`, clamped. A model can send anything here."""
    try:
        limit = int(args.get("limit", _AGENT_SEARCH_DEFAULT))
    except (TypeError, ValueError):
        limit = _AGENT_SEARCH_DEFAULT
    return max(1, min(limit, max_results))


@tool({
        "type": "function",
        "function": {
            "name": "search_agents",
            "description": "Find the user's saved agents — the autonomous workers they have built, each with its own brief, tools and guardrails. Call this before `run_agent` to discover what exists and what each one is for, and when the user refers to an agent by name rather than by id. Returns id, name, description, granted tools and autonomy level.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Words to match against agent name, description and tags. Omit to list all of them."
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Maximum agents to return (default 10, max 25)."
                    }
                },
                "additionalProperties": False
            }
        }
    }, parallel=True, effect="read")
async def search_agents(args: Dict, context: Dict) -> str:
    """List the caller's saved agents, optionally filtered by terms.

    Filtering happens in Python over a bounded scan rather than in the
    database, for the same reason `search_conversation_history` does it:
    part of what is searched (`tags`) is a JSONField, and containment
    lookups on those differ between Postgres and the SQLite the tests run
    on. A user's agent list is small enough that the scan is free.
    """
    from asgiref.sync import sync_to_async
    from agents.models import SubAgent

    user_id = context.get("user_id")
    if not user_id:
        return json.dumps({"error": "No user context."})

    max_results = await alimit(context, "search_agents", "maxResults")
    limit = _agent_search_limit(args, max_results)
    terms = [t for t in re.split(r"\s+", (args.get("query") or "").lower()) if len(t) > 1]

    # None is unrestricted; a selection narrows discovery as well as dispatch,
    # because an agent that can see a name it may not run will keep trying it.
    scope = context.get("delegation_scope")

    def _list() -> list[dict]:
        rows = list(
            SubAgent.objects
            .filter(user_id=user_id)
            .filter(**({} if scope is None else {'id__in': list(scope)}))
            .exclude(status="archived")
            .order_by("-updated_at")
            .values("id", "name", "description", "tags", "status",
                    "tool_grants", "guardrails", "execution_count",
                    "last_executed_at")[:AGENT_SEARCH_MAX_LIMIT * 4]
        )
        matched = [
            row for row in rows
            if not terms or any(t in _agent_haystack(row) for t in terms)
        ]
        return [_agent_summary(row) for row in matched[:limit]]

    try:
        agents = await sync_to_async(_list)()
    except Exception as e:  # noqa: BLE001
        logger.error(f"search_agents failed: {e}")
        return json.dumps({"error": f"Agent search failed: {e}"})

    if not agents:
        return json.dumps({
            "agents": [],
            "message": (
                "This user has no saved agents matching that. Do not invent an "
                "agent_id -- say what you found and offer to do the work yourself."
            ),
        })

    return json.dumps({"agents": agents, "count": len(agents)})


@tool({
        "type": "function",
        "function": {
            "name": "run_agent",
            "description": "Run one of the user's saved agents against a goal and return what it produced. The agent runs with its own tools and guardrails, not yours, and spends against its own budget. Use it to delegate a whole task the agent was built for — not as a way to reach a tool you were not given. Get the id from `search_agents` first; never guess one. Long runs return an execution_id instead of an answer, which you then pass to `get_agent_run`.",
            "parameters": {
                "type": "object",
                "properties": {
                    "agent_id": {
                        "type": "integer",
                        "description": "The agent's id, as returned by search_agents."
                    },
                    "goal": {
                        "type": "string",
                        "description": "What the agent should accomplish, stated as a complete instruction. It cannot see this conversation, so include everything it needs."
                    },
                    "wait_seconds": {
                        "type": "integer",
                        "description": "How long to wait for the result before handing back an execution_id (default 60, max 90)."
                    }
                },
                "required": [
                    "agent_id",
                    "goal"
                ],
                "additionalProperties": False
            }
        }
    },
    sensitive=True,
    effect="irreversible",
)
async def run_agent(args: Dict, context: Dict) -> str:
    """Start one of the caller's agents and wait briefly for its answer.

    Started in the background and *then* awaited, rather than run inline.
    An agent run is research-shaped — up to 40 tool iterations — and the
    chat loop cancels a tool call at `TOOL_CALL_TIMEOUT`. Run inline, that
    cancellation would kill the agent mid-step and leave its ExecutionLog
    stuck at 'running' forever. Detached, the deadline only ends *waiting*:
    the run continues, and its execution_id comes back so the model (and the
    canvas) can still follow it.
    """
    from asgiref.sync import sync_to_async
    from django.contrib.auth import get_user_model

    from agents.agent.runtime import AgentRunRefused, start_agent_run
    from agents.models import SubAgent

    from llm.access import LLMUnavailable

    user_id = context.get("user_id")
    if not user_id:
        return json.dumps({"error": "No user context."})

    goal = (args.get("goal") or "").strip()
    if not goal:
        return "Error: 'goal' is required — tell the agent what to accomplish."

    try:
        agent_id = int(args.get("agent_id"))
    except (TypeError, ValueError):
        return "Error: 'agent_id' must be the numeric id from search_agents."

    try:
        wait = int(args.get("wait_seconds", AGENT_RUN_DEFAULT_WAIT))
    except (TypeError, ValueError):
        wait = AGENT_RUN_DEFAULT_WAIT
    wait = max(0, min(wait, AGENT_RUN_MAX_WAIT))

    # Re-checked here and not only in `search_agents`: the model names ids it
    # saw in earlier turns, and "we didn't list it" has never been access
    # control. The refusal says which boundary was hit, because one that does
    # not is retried until the iteration cap ends the run.
    scope = context.get("delegation_scope")
    if scope is not None and agent_id not in scope:
        return json.dumps({
            "error": (
                f"Agent {agent_id} is not one this agent may delegate to. Call "
                f"search_agents to see the ones it can."
            )
        })

    agent = await SubAgent.objects.filter(
        id=agent_id, user_id=user_id
    ).afirst()
    if agent is None:
        return json.dumps({
            "error": (
                f"No agent {agent_id} belongs to this user. Call search_agents "
                f"for the real ids."
            )
        })

    user = await get_user_model().objects.filter(id=user_id).afirst()
    if user is None:
        return json.dumps({"error": "User not found."})

    try:
        execution_id = await start_agent_run(
            agent, goal, user=user, trigger_type="api", caller="chat",
            # The step that asked for this run. It is what makes the run
            # traceable back to the reasoning that chose to start it, instead
            # of appearing in the history with no explanation of who wanted it.
            parent_step_id=await _parent_step_id(context),
            delegation_task=goal,
            # The caller's write folder, as `invoke_subagent` hands its workers:
            # without it this door could only ever return prose, and a deck or
            # workbook the agent made would land in its own home where the
            # conversation that asked for it never looks.
            workspace=tuple(getattr(context.get("file_scope"), "write_prefix", None) or ()),
        )
    except AgentRunRefused as exc:
        # A guardrail said no — spend cap, disabled agent. The user can act
        # on this, so it is reported rather than retried.
        return json.dumps({"error": str(exc), "refused": True})
    except LLMUnavailable as exc:
        # The agent's provider has no credential behind it. Reported as a
        # refusal for the same reason: retrying cannot help, and the message
        # names the provider and the fix, which is what the model should
        # relay instead of "the agent failed".
        return json.dumps({"error": str(exc), "refused": True})
    except Exception as e:  # noqa: BLE001
        logger.exception("run_agent failed to start agent %s", agent_id)
        return json.dumps({"error": f"Could not start the agent: {e}"})

    outcome = await _await_agent_run(execution_id, wait)
    return json.dumps({
        "type": "agent_run",
        "agent_id": agent_id,
        "agent_name": agent.name,
        "execution_id": execution_id,
        **outcome,
    })


@tool({
        "type": "function",
        "function": {
            "name": "get_agent_run",
            "description": "Check an agent run that had not finished when `run_agent` returned. Reports its status, what it was asked to do, todo/task progress so far and, once it is done, the agent's answer. A run that is still going is not stuck — say so and offer to check again rather than calling this repeatedly.",
            "parameters": {
                "type": "object",
                "properties": {
                    "execution_id": {
                        "type": "string",
                        "description": "The execution_id returned by run_agent."
                    }
                },
                "required": [
                    "execution_id"
                ],
                "additionalProperties": False
            }
        }
    }, parallel=True, effect="read")
async def get_agent_run(args: Dict, context: Dict) -> str:
    """Report where an agent run got to, for a run that outlived its call."""
    from asgiref.sync import sync_to_async
    from agents.models import HITLRequest
    from logs.models import AgentStep, AgentTurn, ExecutionLog

    from .runs import progress_for

    user_id = context.get("user_id")
    execution_id = (args.get("execution_id") or "").strip()
    if not execution_id:
        return "Error: 'execution_id' is required."

    @sync_to_async
    def _read():
        try:
            log = (
                ExecutionLog.objects
                .select_related('subagent')
                .filter(execution_id=execution_id, user_id=user_id)
                .first()
            )
        except (ValueError, ValidationError):
            return None
        if log is None:
            return None
        last = (AgentTurn.objects
                .filter(execution=log).order_by('-index')
                .values_list('reasoning', flat=True).first() or '')
        approval = HITLRequest.objects.filter(
            execution_id=log.id, status='pending').exists()
        # Counts are read here, inside the sync thread: touching relations
        # from the async caller raises SynchronousOnlyOperation. The row
        # itself (for the checkpointer read below) is safe to carry out —
        # only lazy relations are forbidden across the boundary.
        return (log,
                AgentTurn.objects.filter(execution=log).count(),
                AgentStep.objects.filter(execution=log).count(),
                last, approval)

    try:
        # Ownership before status: an execution id is a UUID, but "hard to
        # guess" is not access control, and the answer body is user data.
        if not user_id:
            return json.dumps({"error": "No such run for this user."})
        read = await _read()
        if read is None:
            return json.dumps({"error": "No such run for this user."})
    except (ValueError, ValidationError):
        return json.dumps({"error": "That is not a valid execution_id."})

    log, turns, steps, last_reasoning, needs_approval = read
    outcome = await _await_agent_run(execution_id, 0)
    live = None
    if log.status in ('running', 'paused') and log.thread_id:
        from .runs import live_todos

        live = await live_todos(log.thread_id)
    progress = progress_for(log, turns=turns, steps=steps,
                            last_reasoning=last_reasoning,
                            live_todos=live)
    progress['agent'] = (
        log.subagent.name if log.subagent_id and log.subagent else None)
    progress['needs_approval'] = needs_approval
    return json.dumps({"type": "agent_run", "execution_id": execution_id,
                       **outcome, "progress": progress})


@tool({
    "type": "function",
    "function": {
        "name": SUBAGENT_ANSWER_TOOL,
        "description": (
            "Answer an agent you started that stopped to ask something (listed "
            "under `waiting_on` by run_agent, get_agent_run or invoke_subagent). "
            "For a question pass `answer` — an option's exact text, a number, or "
            "a sentence; if only the user knows, ask them with ask_user first. "
            "For an approval pass `decision`: approve lets the agent run that "
            "call, reject refuses it (give a `reason` it can act on). Rejecting "
            "and answering questions are yours to decide; approving may be "
            "shown to the user, whose answer then stands. The agent resumes and "
            "its result, or its next request, comes back."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "execution_id": {"type": "string", "description": "The paused run's execution_id."},
                "call_id": {"type": "string", "description": "The request's call_id from waiting_on."},
                "decision": {"type": "string", "enum": ["approve", "reject"],
                             "description": "For an approval."},
                "answer": {
                    "description": "For a question: the option text, a list of options, a number or a sentence.",
                    "anyOf": [{"type": "string"}, {"type": "number"},
                              {"type": "array", "items": {"type": "string"}}],
                },
                "reason": {"type": "string", "description": "Why, for a rejection. The agent reads it."},
                "wait_seconds": {"type": "integer",
                                 "description": "How long to wait for the resumed run (default 60, max 90)."},
            },
            "required": ["execution_id", "call_id"],
            "additionalProperties": False,
        },
    },
}, sensitive=True, effect="irreversible")
async def answer_subagent(args: Dict, context: Dict) -> str:
    """Answer a paused worker and resume it — the manager's half of HITL.

    Goes through the same three steps as the Inbox and the approve/reject
    views — record the decision in the checkpoint, close the queue row, resume
    on the original execution id — because a second way to answer is a second
    place for ownership and the resume to drift apart.
    """
    from agents.agent.hitl import resolve_request
    from agents.agent.runtime import resume_agent_run
    from chat.turn.agent import answer_question, approve_tool_call, reject_tool_call

    user_id = context.get("user_id")
    execution_id = str(args.get("execution_id") or "").strip()
    call_id = str(args.get("call_id") or "").strip()
    row = await _pending_row(execution_id, call_id, user_id)
    if row is None:
        return json.dumps({"error": (
            "Nothing is waiting under that execution_id and call_id — it may "
            "already be answered. Call get_agent_run to see where the run is."
        )})
    log = row.execution
    agent = log.subagent
    thread_id = log.thread_id or (row.context_data or {}).get("thread_id") or ""
    if agent is None or not thread_id:
        return json.dumps({"error": "That run can no longer be resumed."})

    try:
        wait = int(args.get("wait_seconds", AGENT_RUN_DEFAULT_WAIT))
    except (TypeError, ValueError):
        wait = AGENT_RUN_DEFAULT_WAIT
    wait = max(0, min(wait, AGENT_RUN_MAX_WAIT))
    decided_by = "user" if context.get("decided_by_user") else "orchestrator"

    if row.request_type == 'clarification':
        if args.get("answer") in (None, "", []):
            return json.dumps({"error": "This is a question: pass `answer`."})
        recorded, problem = await answer_question(thread_id, call_id, args.get("answer"))
        if not recorded:
            return json.dumps({"error": f"The answer was not accepted: {problem}"})
        outcome_label, status = "answered", "answered"
    else:
        decision = str(args.get("decision") or "").strip().lower()
        if decision not in ("approve", "reject"):
            return json.dumps({"error": "This is an approval: pass decision approve or reject."})
        if decision == "approve":
            await approve_tool_call(thread_id, call_id, scope="once",
                                    session_key=thread_id, user_id=user_id)
            outcome_label, status = "approved", "approved"
        else:
            reason = str(args.get("reason") or "").strip()[:500]
            await reject_tool_call(thread_id, call_id, reason=reason, user_id=user_id)
            outcome_label, status = "rejected", "rejected"

    await resolve_request(thread_id=thread_id, call_id=call_id, user_id=user_id,
                          status=status)
    user = await _user(user_id)
    if user is None:
        return json.dumps({"error": "User not found."})
    resumed = await resume_agent_run(agent, user=user, thread_id=thread_id)
    if resumed is None:
        return json.dumps({"error": "The decision was recorded but the run could not be resumed."})

    outcome = await _await_agent_run(resumed, wait)
    return json.dumps({
        "type": "agent_run", "agent_id": agent.id, "agent_name": agent.name,
        "execution_id": resumed, "request": outcome_label, "decided_by": decided_by,
        **outcome,
    })


async def _user(user_id: Any):
    from django.contrib.auth import get_user_model

    if not user_id:
        return None
    return await get_user_model().objects.filter(id=user_id).afirst()


@tool({
    "type": "function",
    "function": {
        "name": "invoke_subagent",
        "description": (
            "Delegate work to the user's specialised agents and wait for their "
            "answers. Give each worker one self-contained task: they run in "
            "parallel, in isolation, and cannot see this conversation or each "
            "other's results. Use this when a job splits cleanly into "
            "independent parts, or when a saved agent is specialised for it. "
            "Prefer doing simple work yourself — every worker costs a full "
            "model run."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "agent_id": {
                    "type": "integer",
                    "description": (
                        "The saved agent to run each task with, from "
                        "search_agents. Omit to use the caller's own "
                        "configuration for the workers."
                    ),
                },
                "tasks": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "One instruction per worker, stating what that worker "
                        "alone must do. A worker sees only its own task and the "
                        "shared `briefing`, never the conversation it came from."
                    ),
                },
                "briefing": {
                    "type": "string",
                    "description": (
                        "Background every worker needs — findings so far, "
                        "constraints, definitions, the format you want back. "
                        "Sent to each worker once. Put shared context here "
                        "rather than repeating it in every task: a task is paid "
                        "for in one worker's context window, this is paid for "
                        "once."
                    ),
                },
            },
            "required": ["tasks"],
            "additionalProperties": False,
        },
    },
}, sensitive=True, effect="irreversible")
async def invoke_subagent(args: Dict, context: Dict) -> str:
    """Fan work out to N workers and return their answers, in order.

    The depth check happens here rather than inside the runtime because this
    is where the decision is made — refusing at the point of asking gives the
    model a message it can act on, instead of N runs that each fail.
    """
    from asgiref.sync import sync_to_async
    from django.contrib.auth import get_user_model

    from agents.agent.orchestrator import (
        DelegationRefused,
        WorkerResult,
        check_delegation_payload,
        check_depth,
        divide_budget,
        run_fanout,
        worker_grants,
    )
    from agents.models import SubAgent

    user_id = context.get("user_id")
    if not user_id:
        return json.dumps({"error": "No user context."})

    depth = int(context.get("depth", 0) or 0)
    try:
        check_depth(depth)
    except DelegationRefused as exc:
        return json.dumps({"error": str(exc), "refused": True})

    tasks = [str(t).strip() for t in (args.get("tasks") or []) if str(t).strip()]
    if not tasks:
        return "Error: 'tasks' must contain at least one instruction."

    briefing = str(args.get("briefing") or "").strip()
    try:
        # Refused, not truncated, and refused *before* any worker starts: a
        # trimmed instruction is a worker doing the wrong job confidently, and
        # the model that wrote the tasks can be told to shorten them and try
        # again. Results have been bounded since the fan-out existed; what goes
        # down was not, and it is the direction that multiplies by worker count.
        check_delegation_payload(tasks, briefing)
    except DelegationRefused as exc:
        return json.dumps({"error": str(exc), "refused": True})

    user = await get_user_model().objects.filter(id=user_id).afirst()
    if user is None:
        return json.dumps({"error": "User not found."})

    worker_agent = None
    # The parent's file/command limits, intersected with the worker's own
    # below and handed to `run_agent` explicitly. Passed as kwargs rather than
    # read back off the mutated row because the "may write nothing" state
    # (`()` from disjoint restrictions) cannot survive a store-and-read cycle:
    # at rest `[]` means unrestricted, so reading it back would widen exactly
    # the workers the intersection refused.
    narrowed_paths: tuple[str, ...] | None = None
    narrowed_commands: tuple[str, ...] | None = None
    if args.get("agent_id") is not None:
        try:
            agent_id = int(args["agent_id"])
        except (TypeError, ValueError):
            return "Error: 'agent_id' must be the numeric id from search_agents."
        scope = context.get("delegation_scope")
        if scope is not None and agent_id not in scope:
            return json.dumps({
                "error": (
                    f"Agent {agent_id} is not one this agent may delegate to. "
                    f"Call search_agents to see the ones it can."
                )
            })
        worker_agent = await SubAgent.objects.filter(
            id=agent_id, user_id=user_id
        ).afirst()
        if worker_agent is None:
            return json.dumps({
                "error": f"No agent {agent_id} belongs to this user."
            })
        # Workers never inherit the right to delegate, whatever the saved row
        # says — see `orchestrator.worker_grants`.
        worker_agent.tool_grants = worker_grants(worker_agent.tool_grants)
        # Nor may a worker widen its parent's per-tool rules. The parent's
        # map flows down most-restrictive-wins, so a parent denied
        # `delete_file` cannot get the deletion by proxy — the same hole
        # `delegation_scope` closed for whole agents.
        parent_permissions = context.get("tool_permissions") or {}
        if isinstance(parent_permissions, dict) and parent_permissions:
            from agents.agent.runtime import (
                merge_tool_permissions,
                tool_permissions_for,
            )
            merged = merge_tool_permissions(
                parent_permissions, tool_permissions_for(worker_agent))
            if merged != tool_permissions_for(worker_agent):
                worker_agent.agent_context = dict(
                    worker_agent.agent_context or {}, toolPermissions=merged)
        # Nor its file or command limits. A worker never holds wider
        # `writePaths`/`commandScope` than whoever started it: the parent's
        # lists intersect the worker's own, most-restrictive-wins, so a lead
        # confined to `src/api/**` cannot field an implementer that writes
        # anywhere. Empty means unrestricted on the way in.
        from agents.agent.runtime import (
            command_scope_for,
            intersect_command_scope,
            intersect_write_paths,
            write_paths_for,
        )
        parent_paths = context.get("write_paths")
        parent_commands = context.get("command_scope")
        if parent_paths is not None or parent_commands is not None:
            narrowed_paths = intersect_write_paths(parent_paths, write_paths_for(worker_agent))
            narrowed_commands = intersect_command_scope(parent_commands, command_scope_for(worker_agent))
            ctx = dict(worker_agent.agent_context or {})
            if narrowed_paths != write_paths_for(worker_agent):
                ctx['writePaths'] = list(narrowed_paths or ())
            if narrowed_commands != command_scope_for(worker_agent):
                ctx['commandScope'] = list(narrowed_commands or ())
            worker_agent.agent_context = ctx

    if worker_agent is None:
        return json.dumps({
            "error": (
                "Ad-hoc workers are not configured yet — pass an `agent_id` "
                "from search_agents to say which agent should run the tasks."
            ),
            "refused": True,
        })

    from agents.agent.runtime import check_guardrails, run_agent
    from agents import budget

    # Reserved and divided before any worker starts. `check_guardrails` reads
    # the spend so far and then permits a run; with N workers starting at once
    # none of them has recorded anything yet, so each would see the full
    # remaining cap and all N would proceed.
    cap = (worker_agent.guardrails or {}).get("spendCapRupees")
    if cap:
        share = divide_budget(cap, len(tasks))
        worker_agent.guardrails = dict(worker_agent.guardrails or {},
                                       spendCapRupees=share)

    # The same reservation for time, made the other way round. Money is
    # *divided* because N concurrent workers' spend adds up; wall-clock is
    # *shared*, because eight workers running for a minute cost one minute and
    # dividing it would cripple each of them while protecting nothing. What the
    # parent does have to keep back is its own last turn — see
    # `Deadline.child`, which is where the reserve lives.
    #
    # Refused up front when there is not enough left, rather than started: N
    # workers that each die on their first model call is a worse answer for the
    # model to read than one sentence telling it to wrap up. A caller with no
    # deadline at all (chat) delegates unbounded, exactly as before.
    parent_deadline = context.get("deadline")
    worker_deadline = None
    if parent_deadline is not None:
        try:
            worker_deadline = parent_deadline.child(budget.limit_for(worker_agent))
        except budget.OutOfTime as exc:
            return json.dumps({"error": str(exc), "refused": True})

    parent_step_id = await _parent_step_id(context)

    # The folder this run writes to, handed down so workers can write there
    # too. Derived from the caller's own scope rather than from its name: the
    # shared folder is by definition the one the parent writes to, so there is
    # no second rule to drift — and it works unchanged when the delegator is
    # chat, whose folder is `/Chat/`.
    parent_scope = context.get("file_scope")
    workspace = tuple(getattr(parent_scope, "write_prefix", None) or ())

    async def runner(task: str, index: int, thread_id: str) -> WorkerResult:
        run = await run_agent(
            worker_agent, task, user=user, thread_id=thread_id,
            trigger_type="api", caller="orchestrator", depth=depth + 1,
            # Provenance: which call delegated, what it asked for, and where in
            # the fan-out this worker sat. `parent_step.turn.reasoning` is then
            # the orchestrator's own thinking at the moment it split the work,
            # which is the first thing you want when a worker goes wrong.
            parent_step_id=parent_step_id,
            delegation_task=task,
            delegation_index=index,
            # Shared background, sent once per worker rather than pasted into
            # every task.
            briefing=briefing,
            # The parent's archive, readable by the worker. Without it a parent
            # that curated a detail away can neither restate it in the task nor
            # point the worker at it.
            parent_session_key=str(context.get("session_id") or ""),
            # One deadline object shared by every worker, not one each: they
            # run concurrently against the same instant, which is what makes
            # the fan-out as a whole bounded rather than each worker bounded
            # and the fan-out unbounded in their number.
            deadline=worker_deadline,
            # Write access to the parent's own folder, so a worker can leave a
            # report there and answer with its path instead of its contents.
            workspace=workspace,
            # The intersected file/command limits, enforced rather than
            # re-derived: `run_agent` falls back to the row's own when these
            # are None, which is the same answer — except for the disjoint
            # `()` case, which the row cannot represent (see above).
            write_paths=narrowed_paths,
            command_scope=narrowed_commands,
        )
        return WorkerResult(
            index=index, task=task, answer=run.answer or "",
            tokens=run.tokens, execution_id=run.execution_id,
        )

    parent_thread = context.get("session_id") or "run"
    fanout = await run_fanout(
        tasks, runner=runner, parent_thread=str(parent_thread),
        parallel=(worker_agent.fanout or {}).get("parallel"),
        # Passed so a trimmed worker answer can be archived under *this* run's
        # session key — which is what makes the parent's `read_tool_output`
        # able to fetch it, and what stops the cut text being lost after the
        # parent has already paid a whole worker run for it.
        context=context,
    )

    # A worker that stopped to ask came back with no answer; say what it is
    # waiting on, so the manager can answer it rather than read an empty slot.
    waiting = await pending_requests([w.execution_id for w in fanout.results])
    return json.dumps({
        "type": "subagent_fanout",
        "agent": worker_agent.name,
        **fanout.as_dict(),
        **({"waiting_on": waiting, "message": PAUSED_MESSAGE} if waiting else {}),
    })
