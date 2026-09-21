"""
Session commands: the client-kind half of Tier 1 plus `/skill` (§18.6).

Client commands are handled entirely in the browser (a setting, a panel, a
new conversation) — no model call. They are registered here so `/help` is
complete and one registry answers "what can I type". The frontend implements
each from the same registry payload (`GET /api/chat/commands/`), so the
behaviour lives in one place rather than in a second copy in the browser.

`/skill <name> [text]` is the turn-kind exception in this module: it applies
one of the user's Skills (`skills.Skill.content`) as instructions for this
turn. The skills app exists and nothing in chat uses it — this is the door.
/research pins the `research` intent (`deep_research`), same as the intent pill.
"""
from __future__ import annotations

import logging

from asgiref.sync import sync_to_async

from .registry import Arg, CommandCall, CommandContext, CommandResult, command

logger = logging.getLogger(__name__)


@command(
    name="help",
    summary="Everything you can run, grouped",
    kind="client", group="general", guest=True,
)
async def help_command(call: CommandCall, ctx: CommandContext) -> CommandResult:
    return CommandResult(status="ok", card={"type": "help"})


@command(
    name="new",
    summary="Start a new conversation",
    kind="client", group="general", guest=True, aliases=["clear"],
)
async def new_command(call: CommandCall, ctx: CommandContext) -> CommandResult:
    return CommandResult(status="ok", card={"type": "new"})


@command(
    name="mode",
    summary="Switch chat autonomy: ask, auto or plan",
    args=[Arg("text", kind="text", required=False, hint="ask, auto or plan")],
    kind="client", group="general",
)
async def mode_command(call: CommandCall, ctx: CommandContext) -> CommandResult:
    level = str(call.args.get("text") or call.text or "").strip().lower()
    if level and level not in ("ask", "auto", "plan"):
        return CommandResult(
            status="error",
            message=f"'{level}' is not a chat mode. Use ask, auto or plan.",
        )
    return CommandResult(
        status="ok", args={"mode": level} if level else {},
        card={"type": "mode", "mode": level} if level else {"type": "mode_cycle"},
    )


@command(
    name="model",
    summary="Switch the model for this conversation",
    args=[Arg("model", kind="model", required=False, hint="Pick a model.")],
    kind="client", group="general",
)
async def model_command(call: CommandCall, ctx: CommandContext) -> CommandResult:
    value = call.args.get("model")
    if not value:
        return CommandResult(status="ok", card={"type": "model_pick"})
    return CommandResult(
        status="ok", args={"model": value}, card={"type": "model", "model": value}
    )


@command(
    name="effort",
    summary="Switch reasoning effort for this conversation",
    args=[Arg("text", kind="text", required=False,
              hint="none, minimal, low, medium or high.")],
    kind="client", group="general",
)
async def effort_command(call: CommandCall, ctx: CommandContext) -> CommandResult:
    from llm.effort import normalize

    level = str(call.args.get("text") or call.text or "").strip()
    if not level:
        return CommandResult(status="ok", card={"type": "effort_pick"})
    # `""` is an explicit request for the model's default and clears a stored
    # level — the one-way-knob bug the effort funnel pins.
    if level == '""' or level.lower() in ("default", "auto-default", "off"):
        return CommandResult(status="ok", args={"effort": ""},
                             card={"type": "effort", "effort": ""})
    resolved = normalize(level)
    if resolved is None:
        return CommandResult(
            status="error",
            message=f"'{level}' is not an effort level (none, minimal, low, medium, high).",
        )
    return CommandResult(
        status="ok", args={"effort": resolved},
        card={"type": "effort", "effort": resolved},
    )


@command(
    name="skill",
    summary="Apply one of your Skills as instructions for this turn",
    args=[
        Arg("skill", kind="skill", required=True, hint="Which skill to apply."),
        Arg("task", kind="text", required=False, hint="What to do with it."),
    ],
    kind="turn", group="agents",
)
async def skill_command(call: CommandCall, ctx: CommandContext) -> CommandResult:
    from django.db.models import Q

    from skills.models import Skill

    skill_id = call.args.get("skill")
    task = str(call.args.get("task") or call.text or "").strip()
    try:
        wanted = int(skill_id)
    except (TypeError, ValueError):
        return CommandResult(status="error", message="Pick one of your skills first.")

    def _load():
        return Skill.objects.filter(
            Q(user_id=ctx.user_id) | Q(is_shared=True), id=wanted
        ).values("id", "title", "content").first()

    row = await sync_to_async(_load)()
    if row is None:
        return CommandResult(status="error", message="No such skill is visible to you.")
    content = str(row.get("content") or "").strip()
    if not content:
        return CommandResult(
            status="error", message=f'Skill "{row.get("title")}" has no content to apply.'
        )
    context_block = (
        f"[COMMAND /skill {row.get('title')}]\n"
        f"The user applied their saved skill \"{row.get('title')}\" as "
        f"instructions for this turn. Follow it exactly; it outranks your "
        f"default working method where they conflict.\n\n"
        f"SKILL:\n{content[:8000]}"
        + (f"\n\nTASK:\n{task}" if task else "")
    )
    return CommandResult(
        status="ok",
        args={"skill_id": row["id"], "skill_title": row.get("title"), "task": task},
        context_block=context_block,
    )


@command(
    name="research",
    summary="Pin a deep-research turn on this question",
    args=[Arg("text", kind="text", required=False, hint="What to research.")],
    kind="turn", group="general", guest=True,
)
async def research_command(call: CommandCall, ctx: CommandContext) -> CommandResult:
    question = str(call.args.get("text") or call.text or "").strip()
    if not question:
        return CommandResult(
            status="error", message="What should be researched? Add the question."
        )
    return CommandResult(
        status="ok",
        args={"question": question},
        context_block=(
            "[COMMAND /research]\nThe user pinned a deep-research turn. Call "
            "`deep_research` first — it plans queries, searches and reads the "
            "pages in one step — then synthesise across sources, surface "
            "disagreements, and cite each claim."
        ),
        intent="research",
    )


@command(
    name="status",
    summary="Running runs, missions, pending approvals, pauses",
    kind="action", group="general",
)
async def status_command(call: CommandCall, ctx: CommandContext) -> CommandResult:
    card = await _status_card(ctx)
    return CommandResult(status="ok", card={"type": "status", **card})


async def _status_card(ctx: CommandContext) -> dict:
    from agents.models import HITLRequest
    from logs.models import ExecutionLog
    from missions.models import Mission

    @sync_to_async
    def _load():
        runs = list(
            ExecutionLog.objects.filter(user_id=ctx.user_id, status__in=("running", "paused"))
            .order_by("-created_at")
            .values("execution_id", "status", "subagent_id")[:10]
        )
        missions = list(
            Mission.objects.filter(user_id=ctx.user_id)
            .exclude(status__in=("done", "failed", "cancelled"))
            .order_by("-created_at")
            .values("id", "goal", "status", "runs_done", "budget_inr", "spent_inr")[:10]
        )
        pending = list(
            HITLRequest.objects.filter(user_id=ctx.user_id, status="pending")
            .order_by("-created_at")
            .values("request_id", "title")[:10]
        )
        paused_until = None
        try:
            from core.models import UserProfile

            profile = UserProfile.objects.filter(user_id=ctx.user_id).first()
            paused_until = getattr(profile, "paused_until", None)
        except Exception:  # noqa: BLE001
            paused_until = None
        return {
            "runs": [{**r, "execution_id": str(r["execution_id"])} for r in runs],
            "missions": missions,
            "pending_approvals": [
                {**p, "request_id": str(p["request_id"])} for p in pending
            ],
            "paused_until": paused_until.isoformat() if paused_until else None,
        }

    return await _load()


@command(
    name="pause",
    summary="Pause everything for a while",
    args=[Arg("duration", kind="duration", required=False,
              hint="How long: 90m, 2h, 3d. Omit to pause until resumed.")],
    kind="action", group="general",
)
async def pause_command(call: CommandCall, ctx: CommandContext) -> CommandResult:
    from datetime import timedelta

    from django.utils import timezone

    seconds = call.args.get("duration")
    until = timezone.now() + timedelta(seconds=int(seconds)) if seconds else None

    @sync_to_async
    def _save():
        from core.models import UserProfile

        profile, _ = UserProfile.objects.get_or_create(user_id=ctx.user_id)
        profile.paused_until = until or (timezone.now() + timedelta(days=365 * 10))
        profile.save(update_fields=["paused_until", "updated_at"])
        return profile.paused_until

    value = await _save()
    try:
        await _cancel_in_flight(ctx)
    except Exception:  # noqa: BLE001
        pass
    return CommandResult(
        status="ok",
        args={"paused_until": value.isoformat()},
        card={"type": "pause", "paused_until": value.isoformat()},
    )


@command(
    name="resume",
    summary="Resume everything",
    kind="action", group="general",
)
async def resume_command(call: CommandCall, ctx: CommandContext) -> CommandResult:
    @sync_to_async
    def _save():
        from core.models import UserProfile

        profile, _ = UserProfile.objects.get_or_create(user_id=ctx.user_id)
        profile.paused_until = None
        profile.save(update_fields=["paused_until", "updated_at"])

    await _save()
    return CommandResult(status="ok", card={"type": "resume"})


async def _cancel_in_flight(ctx: CommandContext) -> None:
    from agents.agent.runtime import cancel_agent_run
    from logs.models import ExecutionLog

    rows = [r async for r in ExecutionLog.objects.filter(
        user_id=ctx.user_id, status__in=("running", "paused"))]
    for row in rows:
        try:
            await cancel_agent_run(row)
        except Exception:  # noqa: BLE001 — one stuck run must not stop the rest
            continue
