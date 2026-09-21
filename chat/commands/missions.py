"""
`/goal` — start a mission from the chat box (§18.5, Tier 1).

`/goal <what you want done>` starts a mission (§10). It opens a confirm
sheet: goal text, agent (defaults to the user's most recent general-purpose
agent, or offers to install one from the gallery), budget (required),
deadline (default 7 days), max runs (default 20). Start calls the
`POST /api/missions/` route, which goes through the same service
`start_mission` uses. The chat then shows a mission card (status, progress
from todos, spend vs budget, next wake) that updates live. `/goal` on its
own lists active missions, and `/goal pause|resume|cancel <id>` are action
commands.

Consent rule: `/goal` creates a budgeted mission — money and days — so it
does nothing until confirmed. The confirm sheet *is* the approval; pressing
Start on it is what the model-created path gets from the `start_mission`
sensitive pause.
"""
from __future__ import annotations

import logging
from datetime import timedelta

from asgiref.sync import sync_to_async
from django.utils import timezone

from .registry import Arg, CommandCall, CommandContext, CommandResult, command

logger = logging.getLogger(__name__)


@command(
    name="goal",
    summary="Start a budgeted multi-run mission",
    args=[
        Arg("text", kind="text", required=False, hint="What you want done."),
        Arg("mission", kind="mission", required=False,
            hint="An existing mission id for pause/resume/cancel."),
    ],
    kind="turn",
    group="goals",
)
async def goal_command(call: CommandCall, ctx: CommandContext) -> CommandResult:
    text = str(call.args.get("text") or call.text or "").strip()
    lowered = text.lower()

    # `/goal pause|resume|cancel <id>` are action commands on an existing row.
    for verb in ("pause", "resume", "cancel"):
        if lowered == verb or lowered.startswith(verb + " "):
            mission_id = call.args.get("mission")
            if mission_id is None:
                tail = text[len(verb):].strip().lstrip("#")
                try:
                    mission_id = int(tail.split()[0]) if tail else None
                except (TypeError, ValueError, IndexError):
                    mission_id = None
            if mission_id is None:
                return CommandResult(
                    status="error",
                    message=f"Which mission? Use /goal {verb} <id> — /goal lists them.",
                )
            return await _mission_action(verb, int(mission_id), ctx)

    if not text:
        # `/goal` on its own lists active missions (an action-style answer
        # carried as a card, no model call needed — but kept `turn` kind so
        # the transcript shows the chip and regenerate replays it).
        missions = await _active_missions(ctx)
        return CommandResult(
            status="ok",
            args={},
            context_block=(
                "[COMMAND /goal]\nThe user asked to see their missions. "
                "Summarise the active ones below from the mission card data; "
                "do not invent any."
            ),
            card={"type": "mission_list", "missions": missions},
        )

    agent = await _default_agent(ctx)
    if agent is None:
        return CommandResult(
            status="confirm",
            message="No agent to run this with yet. Install one from the gallery first.",
            args={"goal": text, "needs_agent": True},
            card={"type": "goal_needs_agent", "goal": text},
        )
    # The confirm sheet shows the resolved arguments; Start is the approval.
    return CommandResult(
        status="confirm",
        message="Review the mission before it starts spending.",
        args={
            "goal": text, "agent_id": agent["id"], "agent_name": agent["name"],
            "budget_inr": 500, "deadline_days": 7, "max_runs": 20,
        },
        card={
            "type": "goal_confirm", "goal": text,
            "agent_id": agent["id"], "agent_name": agent["name"],
        },
    )


async def start_goal_mission(*, user, args: dict) -> dict:
    """Create the mission through the same door `start_mission` uses."""
    from agents.models import SubAgent

    from missions.models import Mission

    goal = str(args.get("goal") or "").strip()
    if not goal:
        raise ValueError("Give the mission goal.")
    try:
        budget = int(args.get("budget_inr") or 0)
    except (TypeError, ValueError):
        raise ValueError("`budget_inr` is required: the most the mission may spend.")
    if budget <= 0:
        raise ValueError("`budget_inr` must be positive.")
    try:
        agent_id = int(args.get("agent_id"))
    except (TypeError, ValueError):
        raise ValueError("Pick the agent that runs this mission.")
    try:
        max_runs = max(1, min(int(args.get("max_runs") or 20), 100))
    except (TypeError, ValueError):
        max_runs = 20
    try:
        days = max(1, min(int(args.get("deadline_days") or 7), 90))
    except (TypeError, ValueError):
        days = 7

    def _create():
        agent = SubAgent.objects.filter(id=agent_id, user=user).first()
        if agent is None:
            raise ValueError(f"No agent {agent_id} belongs to this user.")
        return Mission.objects.create(
            user=user, agent=agent, goal=goal,
            budget_inr=budget, max_runs=max_runs,
            deadline=timezone.now() + timedelta(days=days),
            next_wake_at=timezone.now(),
        )

    row = await sync_to_async(_create)()
    return {
        "mission_id": row.id, "goal": row.goal, "status": row.status,
        "agent_id": agent_id, "budget_inr": budget,
        "max_runs": max_runs, "deadline_days": days,
    }


async def _mission_action(verb: str, mission_id: int, ctx: CommandContext) -> CommandResult:
    from missions.models import Mission

    def _load():
        return Mission.objects.filter(id=mission_id, user_id=ctx.user_id).first()

    row = await sync_to_async(_load)()
    if row is None:
        return CommandResult(status="error", message=f"No mission #{mission_id} is yours.")

    def _save(status: str):
        row.status = status
        row.save(update_fields=["status", "updated_at"])

    if verb == "pause":
        await sync_to_async(_save)("paused")
    elif verb == "cancel":
        await sync_to_async(_save)("cancelled")
    else:
        await sync_to_async(_save)("active")
        await sync_to_async(lambda: Mission.objects.filter(id=row.id).update(
            next_wake_at=timezone.now()))()
    card = await _mission_card(row.id, ctx)
    return CommandResult(
        status="ok", args={"mission_id": row.id, "verb": verb}, card=card,
        context_block=(
            f"[COMMAND /goal {verb} #{row.id}]\nThe mission is now {row.status}. "
            f"Say so in one line from the card data."
        ),
    )


@sync_to_async
def _active_missions(ctx: CommandContext) -> list[dict]:
    from missions.models import Mission

    rows = list(
        Mission.objects.filter(user_id=ctx.user_id)
        .exclude(status__in=("done", "failed", "cancelled"))
        .order_by("-created_at")
        .values("id", "goal", "status", "budget_inr", "spent_inr",
                "runs_done", "max_runs", "next_wake_at")[:20]
    )
    return [
        {**r, "next_wake_at": r["next_wake_at"].isoformat() if r["next_wake_at"] else None}
        for r in rows
    ]


async def _mission_card(mission_id: int, ctx: CommandContext) -> dict:
    from missions.models import Mission

    def _load():
        return Mission.objects.filter(id=mission_id, user_id=ctx.user_id).values(
            "id", "goal", "status", "plan", "budget_inr", "spent_inr",
            "runs_done", "max_runs", "next_wake_at", "last_report",
        ).first()

    row = await sync_to_async(_load)()
    if not row:
        return {"type": "mission", "mission_id": mission_id}
    row["next_wake_at"] = row["next_wake_at"].isoformat() if row["next_wake_at"] else None
    plan = row.get("plan") or []
    open_todos = [t for t in plan if str((t or {}).get("status") or "open").lower()
                  not in ("done", "blocked")]
    return {
        "type": "mission", **row,
        "open_todos": len(open_todos), "total_todos": len(plan),
    }


@sync_to_async
def _default_agent(ctx: CommandContext):
    """The user's most recent general-purpose agent, or None."""
    from agents.models import SubAgent

    row = (
        SubAgent.objects.filter(user_id=ctx.user_id)
        .exclude(status="archived")
        .order_by("-updated_at")
        .values("id", "name")
        .first()
    )
    return dict(row) if row else None
