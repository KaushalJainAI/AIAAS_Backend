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
            .filter(call_id=call_id,
                    execution__input_data__thread_id=session_id)
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


async def _await_agent_run(execution_id: str, wait_seconds: int) -> Dict[str, Any]:
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
                "message": (
                    "The agent paused for human approval of one of its own tool "
                    "calls. It resumes from the agent's run view, not from here."
                ),
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
        "answer": (output.get("answer") or "")[:AGENT_ANSWER_CHAR_LIMIT],
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


def _agent_search_limit(args: Dict) -> int:
    """The caller's `limit`, clamped. A model can send anything here."""
    try:
        limit = int(args.get("limit", AGENT_SEARCH_DEFAULT_LIMIT))
    except (TypeError, ValueError):
        limit = AGENT_SEARCH_DEFAULT_LIMIT
    return max(1, min(limit, AGENT_SEARCH_MAX_LIMIT))


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

    limit = _agent_search_limit(args)
    terms = [t for t in re.split(r"\s+", (args.get("query") or "").lower()) if len(t) > 1]

    def _list() -> list[dict]:
        rows = list(
            SubAgent.objects
            .filter(user_id=user_id)
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
            "description": "Check an agent run that had not finished when `run_agent` returned. Reports its status and, once it is done, the agent's answer. A run that is still going is not stuck — say so and offer to check again rather than calling this repeatedly.",
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
    from logs.models import ExecutionLog

    user_id = context.get("user_id")
    execution_id = (args.get("execution_id") or "").strip()
    if not execution_id:
        return "Error: 'execution_id' is required."

    @sync_to_async
    def _owned() -> bool:
        return ExecutionLog.objects.filter(
            execution_id=execution_id, user_id=user_id
        ).exists()

    try:
        # Ownership before status: an execution id is a UUID, but "hard to
        # guess" is not access control, and the answer body is user data.
        if not user_id or not await _owned():
            return json.dumps({"error": "No such run for this user."})
    except (ValueError, ValidationError):
        return json.dumps({"error": "That is not a valid execution_id."})

    outcome = await _await_agent_run(execution_id, 0)
    return json.dumps({"type": "agent_run", "execution_id": execution_id, **outcome})


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
    if args.get("agent_id") is not None:
        try:
            agent_id = int(args["agent_id"])
        except (TypeError, ValueError):
            return "Error: 'agent_id' must be the numeric id from search_agents."
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
        )
        return WorkerResult(
            index=index, task=task, answer=run.answer or "",
            tokens=run.tokens, execution_id=run.execution_id,
        )

    parent_thread = context.get("session_id") or "run"
    fanout = await run_fanout(
        tasks, runner=runner, parent_thread=str(parent_thread),
        parallel=(worker_agent.fanout or {}).get("parallel"),
    )

    return json.dumps({
        "type": "subagent_fanout",
        "agent": worker_agent.name,
        **fanout.as_dict(),
    })
