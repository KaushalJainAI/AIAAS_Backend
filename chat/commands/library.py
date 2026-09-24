"""
Tier-2 commands: `/schedule`, `/file`, `/cost`, `/summarize`, `/export`,
`/deck`, `/doc`, `/sheet`, `/chart`, `/diagram`, `/pdf`, `/dashboard`,
`/eval` (§18.6).

All built on things that already exist: the trigger preview endpoint's
sentence before saving, VFS completion, `CostEntry` sums, the office
renderers, and `cases/from-run/`. Each either pins an intent/toolbox for a
turn (so the model builds the right artifact) or does one thing server-side
and returns a card (no model call).
"""
from __future__ import annotations

import logging

from asgiref.sync import sync_to_async

from .registry import Arg, CommandCall, CommandContext, CommandResult, command

logger = logging.getLogger(__name__)


@command(
    name="schedule",
    summary="Schedule an agent — 'every weekday at 9' → cron, previewed",
    args=[
        Arg("agent", kind="agent", required=True, hint="Which agent runs."),
        Arg("text", kind="text", required=False, hint="When: 'every weekday at 9'."),
    ],
    kind="action", group="agents",
)
async def schedule_command(call: CommandCall, ctx: CommandContext) -> CommandResult:
    agent_id = call.args.get("agent")
    when = str(call.args.get("text") or call.text or "").strip()
    try:
        wanted = int(agent_id)
    except (TypeError, ValueError):
        return CommandResult(status="error", message="Pick one of your agents first.")
    agent = await _owned_agent(ctx, wanted)
    if agent is None:
        return CommandResult(status="error", message="No agent with that name belongs to you.")
    if not when:
        return CommandResult(
            status="confirm",
            message="When should it run? The preview shows the server's reading before saving.",
            args={"agent_id": wanted, "agent_name": agent["name"]},
            card={"type": "schedule_pick", "agent_id": wanted,
                  "agent_name": agent["name"]},
        )
    cron = _words_to_cron(when)
    if cron is None:
        return CommandResult(
            status="confirm",
            message="Say when in cron or plain words — the preview below is what the server will arm.",
            args={"agent_id": wanted, "agent_name": agent["name"], "when": when},
            card={"type": "schedule_pick", "agent_id": wanted,
                  "agent_name": agent["name"], "when": when},
        )
    preview = await _preview_cron(cron, ctx)
    return CommandResult(
        status="confirm",
        message="Review the schedule before it is armed.",
        args={"agent_id": wanted, "agent_name": agent["name"],
              "cron": cron, "preview": preview},
        card={"type": "schedule_confirm", "agent_id": wanted,
              "agent_name": agent["name"], "cron": cron, "preview": preview},
    )


async def create_schedule_trigger(*, user, agent_id: int, cron: str,
                                  timezone_name: str = "UTC",
                                  name: str = "") -> dict:
    """Arm a manual schedule through the trigger serializer (ownership + the
    allow-unattended pair are validated there, not here)."""
    from agents.models import SubAgent
    from agents.views.triggers import TriggerSerializer, _arm

    def _load():
        return SubAgent.objects.filter(id=agent_id, user=user).first()

    agent = await sync_to_async(_load)()
    if agent is None:
        raise ValueError("No such agent.")

    class _Request:
        def __init__(self, user):
            self.user = user

    form = TriggerSerializer(
        data={"subagent": agent.id, "mode": "schedule", "cron": cron,
              "timezone": timezone_name or "UTC",
              "name": name or "From /schedule",
              "goal": agent.prompt or ""},
        context={"request": _Request(user)},
    )
    form.is_valid(raise_exception=True)
    trigger = await sync_to_async(lambda: form.save(origin="manual"))()
    await sync_to_async(_arm)(trigger)
    return {"trigger_id": trigger.id, "cron": cron}


def _words_to_cron(when: str) -> str | None:
    """A tiny plain-words reader for the common shapes. Cron passes through;
    anything else returns None and the preview endpoint does the checking."""
    import re

    text = (when or "").strip().lower()
    if re.fullmatch(r"(\*|[0-9,\-*/]+)\s+(\*|[0-9,\-*/]+)\s+(\*|[0-9,\-*/]+)\s+(\*|[0-9a-z,\-*/]+)\s+(\*|[0-9a-z,\-*/]+)", text):
        return " ".join(text.split())
    match = re.fullmatch(r"(?:every\s+day\s+at\s+)?(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", text)
    if match:
        hour, minute, meridiem = int(match.group(1)), int(match.group(2) or 0), match.group(3)
        if meridiem == "pm" and hour < 12:
            hour += 12
        if meridiem == "am" and hour == 12:
            hour = 0
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            return f"{minute} {hour} * * *"
    match = re.fullmatch(r"(?:every\s+)?weekday[s]?\s+at\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", text)
    if match:
        hour, minute, meridiem = int(match.group(1)), int(match.group(2) or 0), match.group(3)
        if meridiem == "pm" and hour < 12:
            hour += 12
        if meridiem == "am" and hour == 12:
            hour = 0
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            return f"{minute} {hour} * * 1-5"
    match = re.fullmatch(r"every\s+(\d+)\s*(m|min|mins|minutes|h|hours?)", text)
    if match:
        value, unit = int(match.group(1)), match.group(2)
        if unit.startswith("m"):
            if 1 <= value <= 59:
                return f"*/{value} * * * *" if value > 1 else "* * * * *"
        else:
            if 1 <= value <= 23:
                return f"0 */{value} * * *" if value > 1 else "0 * * * *"
    return None


async def _preview_cron(cron: str, ctx: CommandContext) -> dict:
    from django.utils import timezone as dj_timezone

    from agents.triggers import (
        describe as cron_describe,
        is_valid as cron_is_valid,
        next_runs,
        zone_is_valid,
    )

    tz = await _user_timezone(ctx)
    if not zone_is_valid(tz):
        tz = "UTC"
    if not cron_is_valid(cron):
        return {"valid": False, "error": 'Expected five cron fields, e.g. "0 9 * * 1".'}
    now = dj_timezone.now()
    runs = await sync_to_async(lambda: next_runs(cron, now, tz, count=3))()
    if not runs:
        return {"valid": False, "error": "This schedule has no next run."}
    return {
        "valid": True,
        "description": cron_describe(cron, tz),
        "upcoming": [r.isoformat() for r in runs],
        "timezone": tz,
    }


@sync_to_async
def _user_timezone(ctx: CommandContext) -> str:
    from core.models import UserProfile

    profile = UserProfile.objects.filter(user_id=ctx.user_id).first()
    return (getattr(profile, "timezone", "") or "UTC").strip() or "UTC"


@sync_to_async
def _owned_agent(ctx: CommandContext, agent_id: int):
    from agents.models import SubAgent

    row = SubAgent.objects.filter(id=agent_id, user_id=ctx.user_id).values(
        "id", "name", "allow_unattended").first()
    return dict(row) if row else None


@command(
    name="file",
    summary="Attach a VFS file by path — a chip, not an upload",
    args=[Arg("text", kind="file", required=False, hint="Path under /Chat/ or /Agents/.")],
    kind="client", group="files",
)
async def file_command(call: CommandCall, ctx: CommandContext) -> CommandResult:
    path = str(call.args.get("text") or call.text or "").strip()
    if not path:
        return CommandResult(status="ok", card={"type": "file_pick"})
    return CommandResult(
        status="ok", args={"path": path}, card={"type": "file", "path": path}
    )


@command(
    name="cost",
    summary="This conversation's spend: tokens + ledger by kind",
    kind="action", group="general",
)
async def cost_command(call: CommandCall, ctx: CommandContext) -> CommandResult:
    card = await _cost_card(ctx)
    return CommandResult(status="ok", card={"type": "cost", **card})


async def _cost_card(ctx: CommandContext) -> dict:
    from django.db.models import Sum

    from logs.models import CostEntry

    @sync_to_async
    def _load():
        from chat.models import ChatSession

        session = ChatSession.objects.filter(
            id=ctx.session_id, user_id=ctx.user_id).first() if ctx.session_id else None
        by_kind = list(
            CostEntry.objects.filter(user_id=ctx.user_id)
            .values("kind").annotate(total=Sum("amount_inr"))
        )
        session_cost = None
        if session is not None:
            session_cost = {
                "total_cost_usd": str(session.total_cost_usd),
                "cost_source": session.cost_source,
                "total_tokens_used": session.total_tokens_used,
            }
        return {"by_kind": by_kind, "session": session_cost}

    return await _load()


@command(
    name="summarize",
    summary="Conversation → /Chat/<title> summary.md",
    kind="turn", group="files",
)
async def summarize_command(call: CommandCall, ctx: CommandContext) -> CommandResult:
    return CommandResult(
        status="ok",
        args={},
        context_block=(
            "[COMMAND /summarize]\nSummarise this conversation into a markdown "
            "file at /Chat/<title> summary.md using write_file: what was asked, "
            "what was decided, open items, and where the files it produced live. "
            "Reply with the path and two lines on what it holds."
        ),
    )


@command(
    name="export",
    summary="Conversation to a file: md, docx or pdf",
    args=[Arg("text", kind="text", required=False, hint="md, docx or pdf.")],
    kind="turn", group="files",
)
async def export_command(call: CommandCall, ctx: CommandContext) -> CommandResult:
    fmt = str(call.args.get("text") or call.text or "md").strip().lower()
    if fmt not in ("md", "markdown", "docx", "pdf"):
        return CommandResult(
            status="error", message="Export as md, docx or pdf — pick one."
        )
    if fmt == "markdown":
        fmt = "md"
    tool = {"md": "write_file", "docx": "render_document", "pdf": "render_pdf"}[fmt]
    return CommandResult(
        status="ok",
        args={"format": fmt},
        context_block=(
            f"[COMMAND /export {fmt}]\nExport this conversation to a {fmt} file "
            f"with {tool} under /Chat/: the questions and the answers with their "
            f"sources, in order. Reply with the path."
        ),
        tool_pin=(tool,),
    )


@command(
    name="deck",
    summary="Build a slide deck about this",
    args=[Arg("text", kind="text", required=False, hint="What the deck covers.")],
    kind="turn", group="office",
)
async def deck_command(call: CommandCall, ctx: CommandContext) -> CommandResult:
    topic = str(call.args.get("text") or call.text or "").strip()
    return CommandResult(
        status="ok", args={"topic": topic},
        context_block=(
            "[COMMAND /deck]\nBuild a slide deck with render_deck"
            + (f" about: {topic}" if topic else "")
            + ". One idea per slide, a native chart for numbers over time, "
              "speaker notes for the presenter. Reply with the path."
        ),
        tool_pin=("render_deck",),
    )


@command(
    name="doc",
    summary="Write a Word document about this",
    args=[Arg("text", kind="text", required=False, hint="What the document covers.")],
    kind="turn", group="office",
)
async def doc_command(call: CommandCall, ctx: CommandContext) -> CommandResult:
    topic = str(call.args.get("text") or call.text or "").strip()
    return CommandResult(
        status="ok", args={"topic": topic},
        context_block=(
            "[COMMAND /doc]\nWrite a Word document with render_document"
            + (f" about: {topic}" if topic else "")
            + " — headings, tables, quotes. Reply with the path."
        ),
        tool_pin=("render_document",),
    )


@command(
    name="sheet",
    summary="Build a workbook about this",
    args=[Arg("text", kind="text", required=False, hint="What the workbook holds.")],
    kind="turn", group="office",
)
async def sheet_command(call: CommandCall, ctx: CommandContext) -> CommandResult:
    topic = str(call.args.get("text") or call.text or "").strip()
    return CommandResult(
        status="ok", args={"topic": topic},
        context_block=(
            "[COMMAND /sheet]\nBuild a workbook with render_workbook"
            + (f" for: {topic}" if topic else "")
            + ". Typed columns, totals as formulas (never typed-in numbers). "
              "Reply with the path."
        ),
        tool_pin=("render_workbook",),
    )


@command(
    name="dashboard",
    summary="Draw a dashboard about this",
    args=[Arg("text", kind="text", required=False, hint="What the dashboard shows.")],
    kind="turn", group="office",
)
async def dashboard_command(call: CommandCall, ctx: CommandContext) -> CommandResult:
    topic = str(call.args.get("text") or call.text or "").strip()
    return CommandResult(
        status="ok", args={"topic": topic},
        context_block=(
            "[COMMAND /dashboard]\nDraw a dashboard with render_dashboard"
            + (f" about: {topic}" if topic else "")
            + " — tiles are kpi, chart, table or text, and a chart tile takes "
              "exactly render_chart's spec. Never write HTML for a dashboard."
        ),
        tool_pin=("render_dashboard",),
    )


@command(
    name="chart",
    summary="Draw a chart about this",
    args=[Arg("text", kind="text", required=False, hint="What the chart shows.")],
    kind="turn", group="office",
)
async def chart_command(call: CommandCall, ctx: CommandContext) -> CommandResult:
    topic = str(call.args.get("text") or call.text or "").strip()
    return CommandResult(
        status="ok", args={"topic": topic},
        context_block=(
            "[COMMAND /chart]\nDraw a chart with render_chart"
            + (f" about: {topic}" if topic else "")
            + " — {kind, title, series} as data, at most eight series (fold "
              "the tail into Other), gaps stay gaps, never zeros. Numbers "
              "first: compute in execute_python where they need computing, "
              "then chart them."
        ),
        tool_pin=("render_chart",),
    )


@command(
    name="diagram",
    summary="Draw boxes-and-arrows about this",
    args=[Arg("text", kind="text", required=False, hint="What the diagram shows.")],
    kind="turn", group="office",
)
async def diagram_command(call: CommandCall, ctx: CommandContext) -> CommandResult:
    topic = str(call.args.get("text") or call.text or "").strip()
    return CommandResult(
        status="ok", args={"topic": topic},
        context_block=(
            "[COMMAND /diagram]\nDraw a boxes-and-arrows diagram with "
            "render_diagram"
            + (f" about: {topic}" if topic else "")
            + ". Nodes and edges as data — the renderer owns the layout, so "
              "never hand-author SVG for this. Reply with the path."
        ),
        tool_pin=("render_diagram",),
    )


@command(
    name="pdf",
    summary="Render this as a PDF to send",
    args=[Arg("text", kind="text", required=False, hint="What the PDF holds.")],
    kind="turn", group="office",
)
async def pdf_command(call: CommandCall, ctx: CommandContext) -> CommandResult:
    topic = str(call.args.get("text") or call.text or "").strip()
    return CommandResult(
        status="ok", args={"topic": topic},
        context_block=(
            "[COMMAND /pdf]\nRender a PDF with render_pdf"
            + (f" about: {topic}" if topic else "")
            + " — the same blocks render_document takes (a .docx is what you "
              "edit, a .pdf is what you send). Reply with the path."
        ),
        tool_pin=("render_pdf",),
    )


@command(
    name="eval",
    summary="Save the last exchange as an eval case draft",
    kind="action", group="general",
)
async def eval_command(call: CommandCall, ctx: CommandContext) -> CommandResult:
    from chat.models import ChatMessage

    if not ctx.session_id:
        return CommandResult(status="error", message="Open a conversation first.")

    def _load():
        rows = list(
            ChatMessage.objects.filter(session_id=ctx.session_id)
            .order_by("-created_at")
            .values("id", "role", "content")[:10]
        )
        question = next((r["content"] for r in rows if r["role"] == "user"
                         and (r["content"] or "").strip()), "")
        answer = next((r for r in rows if r["role"] == "assistant"
                       and (r["content"] or "").strip()), None)
        return question, answer

    question, answer = await sync_to_async(_load)()
    if answer is None:
        return CommandResult(status="error", message="No assistant answer to save yet.")
    case = await _save_chat_case(
        ctx, str(question or ""), str(answer.get("content") or ""),
        message_id=answer["id"])
    return CommandResult(
        status="ok",
        card={"type": "eval_saved", "case_id": case["id"],
              "suite": case["suite"], "message_id": answer["id"],
              "draft": True},
    )


@sync_to_async
def _save_chat_case(ctx: CommandContext, question: str, answer: str,
                    message_id: int = 0) -> dict:
    """The user's question as the goal, the saved answer as the reference.

    A draft, like every other model-derived case: the answer is the model's
    own words, so it scores nothing until a person accepts it on the Evals
    page. (Before this, the goal was a stub about the answer and the case
    was active — testing the wrong thing with no review.)
    """
    from eval import api as evals
    from eval.models import EvalSuite

    suite, _ = EvalSuite.objects.get_or_create(
        user_id=ctx.user_id, name="From chat",
        defaults={"description": "Cases saved from chat with /eval.",
                  "supervision": "all"},
    )
    saved = evals.save_cases(suite, [{
        'name': f'From chat {message_id or suite.cases.count()}',
        'goal': (question.strip() or 'Chat question saved for regression.'),
        'reference': answer[:4000],
        'graders': [],
        'tags': ['from-chat'],
    }], drafts=True)
    if not saved:
        raise ValueError('The suite is full.')
    return {"id": saved[0].id, "suite": suite.name}
