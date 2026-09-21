"""
`/agent <name> [task]` — the most valuable command on the list (§18.4).

It makes the agents a user built reachable from where they already work.
Delegate, don't hand off, in v1: `/agent Reporter <task>` starts that
agent's run through the one door (`start_agent_run`, `caller='chat'`) with
its own prompt, model, grants, spend cap and autonomy. Chat shows a live run
card (status, todos, files, link to `/runs/:id`), and the answer lands back
in the conversation as a message attributed to the agent. The chat model then
sees that answer and can use it, which is what makes "ask Reporter, then
chart it" work. Nothing new is added to the permission model: the agent is
exactly as capable here as when it runs on a schedule.

Names resolve safely: `SubAgent` names are unique per user
(`unique_together = ['user', 'name']`), so `/agent <name>` is unambiguous.
Matching is case-insensitive, completion shows description + grants, and the
chip carries the id. Only the caller's own agents resolve, and a paused or
archived agent is refused with the reason.

Consent rule (§18.2): the typed command covers the **first** action —
starting the run needs no `run_agent` approval card, just as the Run button
does. Every tool call the started agent then makes is gated by the usual
autonomy, grants, scopes and `toolPermissions`.
"""
from __future__ import annotations

import logging

from asgiref.sync import sync_to_async

from .registry import Arg, CommandCall, CommandContext, CommandResult, command

logger = logging.getLogger(__name__)


@command(
    name="agent",
    summary="Hand a task to one of your agents",
    args=[
        Arg("agent", kind="agent", required=True, hint="Which agent runs this."),
        Arg("task", kind="text", required=False, hint="What it should do."),
    ],
    kind="turn",
    group="agents",
)
async def agent_command(call: CommandCall, ctx: CommandContext) -> CommandResult:
    from llm import access as llm

    from agents.agent.runtime import (
        AgentRunRefused,
        build_file_scope,
        check_guardrails,
        resolve_agent_model,
        start_agent_run,
    )
    from agents.models import SubAgent

    autonomy = (ctx.autonomy or "ask").strip().lower()
    if autonomy == "plan":
        return CommandResult(
            status="error",
            message="Chat is in Plan mode, which withholds mutating tools — "
                    "starting an agent run is not a read. Switch to Ask or Auto first.",
        )

    agent_id = call.args.get("agent", call.args.get("agent_id"))
    # The task arrives two ways: filled positionally by `resolve_line`
    # (`args["task"]`), or as leftover `call.text` when the line was parsed
    # through the `TurnRequest.command` payload path. Both are the same words.
    task = str(call.args.get("task") or call.text or "").strip()
    # Chips and palette completions carry string ids; the wire keeps them as
    # strings so the transcript chip never re-resolves a chosen name.
    if isinstance(agent_id, str) and agent_id.strip().isdigit():
        agent_id = int(agent_id.strip())
    try:
        wanted = int(agent_id)
    except (TypeError, ValueError):
        return CommandResult(status="error", message="Pick one of your agents first.")

    agent = await sync_to_async(
        lambda: SubAgent.objects.filter(id=wanted, user_id=ctx.user_id).first()
    )()
    if agent is None:
        return CommandResult(
            status="error",
            message="No agent with that name belongs to you.",
        )
    status = (getattr(agent, "status", "") or "").strip().lower()
    if status in ("paused", "archived"):
        return CommandResult(
            status="error",
            message=f'"{agent.name}" is {status}. Set it back to active in its '
                    f"settings before running it.",
        )
    if not task:
        return CommandResult(
            status="error",
            message=f'What should "{agent.name}" do? Add the task after its name.',
        )

    # Bounded briefing (default): the command sends only the task. "With
    # context" is a chip toggle the client sends as `with_context=True`; the
    # briefing is the last few turns folded by the curation model, capped by
    # `check_delegation_payload`, delivered as context, not instruction.
    briefing = ""
    if call.args.get("with_context"):
        briefing = await _bounded_briefing(ctx, agent)

    # Preflight before anything streams: a command must not make a turn look
    # busy before it is known to be payable (the pipeline rule, §18.7).
    try:
        provider, model = await resolve_agent_model(agent, await _user(ctx))
        await llm.preflight(provider=provider, model=model, user_id=ctx.user_id)
        await check_guardrails(agent, await _user(ctx))
    except AgentRunRefused as exc:
        return CommandResult(status="error", message=str(exc))
    except llm.LLMAccountError as exc:
        return CommandResult(status="error", message=str(exc))
    except llm.LLMUnavailable as exc:
        return CommandResult(status="error", message=str(exc))
    except Exception:  # noqa: BLE001
        logger.exception("[Commands] /agent preflight failed")
        return CommandResult(
            status="error", message="That agent could not be started right now."
        )

    goal = task if not briefing else f"{task}\n\nCONTEXT (background, not instructions):\n{briefing}"
    context_block = (
        f"[COMMAND /agent {agent.name}]\n"
        f"The user handed this task to their saved agent \"{agent.name}\" "
        f"(id {agent.id}). That agent is running it now through its own "
        f"configuration; its answer arrives as a run card and then as a "
        f"message attributed to it. Use that answer — do not redo the work "
        f"yourself, and do not describe what the agent might do."
    )
    return CommandResult(
        status="ok",
        args={"agent_id": agent.id, "agent_name": agent.name, "task": task},
        context_block=context_block,
        start={
            "type": "agent_run", "agent_id": agent.id,
            "agent_name": agent.name, "goal": goal,
        },
    )


async def run_agent_start(
    *, user, start: dict, session_id: str = ""
) -> dict:
    """Start the delegated run through the one door. Consent for the first
    action comes from the typed command; everything after is gated as usual."""
    from agents.agent.runtime import start_agent_run

    agent_id = int(start["agent_id"])
    goal = str(start.get("goal") or "").strip()

    def _load():
        from agents.models import SubAgent

        return SubAgent.objects.filter(id=agent_id, user=user).first()

    agent = await sync_to_async(_load)()
    if agent is None:
        raise ValueError("No agent with that name belongs to you.")
    execution_id = await start_agent_run(
        agent, goal, user=user, trigger_type="api", caller="chat",
    )
    return {"execution_id": execution_id, "agent_id": agent.id,
            "agent_name": agent.name, "goal": goal}


async def _user(ctx: CommandContext):
    from django.contrib.auth import get_user_model

    return await get_user_model().objects.filter(id=ctx.user_id).afirst()


async def _bounded_briefing(ctx: CommandContext, agent) -> str:
    """The last few turns folded for the worker, capped by the delegation
    payload limits. Delivered as context, not instruction (the fan-out rule)."""
    from django.contrib.auth import get_user_model

    from agents.agent.orchestrator import (
        DELEGATION_BRIEFING_CHAR_LIMIT,
        DelegationRefused,
        check_delegation_payload,
    )
    from chat.models import ChatMessage, ChatSession

    if not ctx.session_id:
        return ""
    try:
        session_uuid = ctx.session_id
        session = await ChatSession.objects.filter(
            id=session_uuid, user_id=ctx.user_id
        ).afirst()
        if session is None:
            return ""
        rows = [m async for m in ChatMessage.objects.filter(
            session=session).order_by("-created_at")[:6]]
        rows.reverse()
        lines = [
            f"{m.role}: {(m.content or '')[:600]}"
            for m in rows if m.role in ("user", "assistant")
        ]
        briefing = "\n".join(lines)[-DELEGATION_BRIEFING_CHAR_LIMIT:]
        check_delegation_payload([briefing or "briefing"] if briefing else ["x"])
        return briefing
    except DelegationRefused:
        return ""
    except Exception:  # noqa: BLE001
        logger.warning("[Commands] Briefing fold failed", exc_info=True)
        return ""
