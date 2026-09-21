"""
One chat turn, end to end.

Both endpoints — streaming and plain — run exactly this. They differ only in the
`EventSink` they pass: an SSE bridge, or one that discards. That is the reason
this module exists; the two used to be separate 300- and 860-line
implementations of the same pipeline that had already drifted apart on
attachments, recall and error handling.
"""
from __future__ import annotations

import base64
import json
import logging
import re
import time
from decimal import Decimal
from dataclasses import dataclass, field
from typing import Any, Mapping
from uuid import uuid4

from asgiref.sync import sync_to_async

from workflow_backend.thresholds import (
    ASSISTANT_SUMMARY_WORD_LIMIT,
    FOLLOW_UPS_SLOW_TURN_SECONDS,
    MAX_CONTEXT_TOKENS,
)

from chat import vision
from llm import access as llm
from llm.pricing import combine_sources
from llm.effort import normalize as normalize_effort
from . import agent, history, prompts
from .agent import TurnContext, TurnResult
from .events import Event, EventSink, null_sink
from chat.models import ChatMessage, ChatSession

logger = logging.getLogger(__name__)

#: Intents a client may request explicitly. Anything else is inferred.
SELECTABLE_INTENTS = frozenset({"chat", "search", "research", "image", "video"})

_SLASH_COMMANDS = {
    "/search": "search",
    "/image": "image",
    "/video": "video",
    "/research": "research",
}

#: Openers that reliably mean "look this up", used only when the client did not
#: state an intent. Getting it wrong is cheap — the agent can still search.
_SEARCH_OPENERS = (
    "what is", "who is", "when did", "how to", "latest", "current",
    "news about", "tell me about", "search for", "look up", "find",
    "what are the", "define", "explain", "compare",
)

#: Phrases meaning "this question is about our conversation, not the world".
#: Narrow on purpose: a false positive costs one indexed scan, a false negative
#: is the assistant claiming not to remember something the user just said.
_RECALL_MARKERS = (
    "earlier", "previously", "before", "last time", "you said", "you told me",
    "i said", "i told you", "i mentioned", "we discussed", "we talked about",
    "we decided", "remember", "recall", "you mentioned", "as i mentioned",
    # "my name" rather than "my name is": it must catch the question form
    # ("what is my name") as well as the statement.
    "what did i", "what was the", "remind me", "go back to", "my name",
    "earlier you", "above", "the one i gave", "i gave you",
)

_MEDIA_CAPABILITY = {
    "image": ("supports_image_generation", "image generation"),
    "video": ("supports_video_generation", "video generation"),
}

#: Internal scaffolding that must never reach the user, in case a model echoes
#: the context blocks back at us.
_INTERNAL_TAGS = re.compile(
    r"<context_metadata[^>]*>.*?</context_metadata>"
    r"|\[FULL RESOURCE CONTENT\]|\[END FULL CONTENT\]"
    r"|\[SOURCES ALREADY REVIEWED\]",
    re.DOTALL | re.IGNORECASE,
)


class TurnError(Exception):
    """The turn cannot proceed; the message is safe to show the user."""


# ── Request ──────────────────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class TurnRequest:
    """A validated inbound chat request."""

    content: str = ""
    intent: str | None = None
    provider: str | None = None
    model: str | None = None
    #: How hard to think, from `llm.effort.LADDER`. None means the client did
    #: not say, and the session's stored choice stands — which is what makes a
    #: client that has never heard of effort behave exactly as before.
    effort: str | None = None
    reference_message_id: int | None = None
    approve_tool_call: str | None = None
    #: "and stop asking me about this tool". Only meaningful with an approval.
    #: The retired spelling of `approval_scope='always'`, still accepted because
    #: clients in the wild send it.
    remember_approval: bool = False
    #: How long the approval lasts: 'once' | 'session' | 'always'. Empty means
    #: the client did not say, and `approve_tool_call` falls back to reading
    #: `remember_approval`.
    approval_scope: str = ""
    #: The mirror of `approve_tool_call`. Chat's Deny button used to clear the
    #: card and nothing else, so the graph stayed parked on its `interrupt()`
    #: and the model was never told it had been refused — the same asymmetry
    #: `agent_reject` exists to close for agent runs.
    reject_tool_call: str | None = None
    reject_reason: str = ""
    regenerate_of: int | None = None
    #: A slash command the client resolved through the palette
    #: (`TurnRequest.command = {"name", "args", "text"}`). The backend
    #: re-resolves it: chips carry ids, but a typed line is parsed here, so
    #: both arrive validated rather than trusted.
    command: dict[str, Any] | None = None

    @classmethod
    def parse(cls, payload: Mapping[str, Any]) -> TurnRequest:
        """Read a request body, rejecting anything unusable up front."""
        approval = (payload.get("approve_tool_call") or "").strip() or None
        rejection = (payload.get("reject_tool_call") or "").strip() or None
        content = (payload.get("content") or "").strip()
        if not content and not approval and not rejection:
            raise TurnError("A message is required.")

        requested = (payload.get("intent") or "").strip().lower()
        reference = payload.get("reference")

        try:
            regenerate_of = payload.get("regenerate_of")
            regenerate_of = int(regenerate_of) if regenerate_of is not None else None
        except (TypeError, ValueError):
            regenerate_of = None
        command = payload.get("command")
        if command is not None and not isinstance(command, dict):
            raise TurnError("'command' must be an object.")
        return cls(
            content=content,
            regenerate_of=regenerate_of,
            command=dict(command) if isinstance(command, dict) else None,
            intent=requested if requested in SELECTABLE_INTENTS else None,
            provider=(payload.get("llm_provider") or "").strip() or None,
            model=(payload.get("llm_model") or "").strip() or None,
            # Validated here rather than trusted: an unknown level would reach
            # `llm.effort.resolve` and be ignored, but silently ignoring a
            # value the client believes it set is how a knob becomes
            # decorative. Unknown reads as "not specified".
            effort=_parse_effort(payload.get("llm_effort")),
            reference_message_id=(
                reference.get("message_id") if isinstance(reference, dict) else None
            ),
            approve_tool_call=approval,
            remember_approval=bool(payload.get("remember_approval")) and approval is not None,
            approval_scope=(
                (payload.get("approval_scope") or "").strip().lower()
                if approval is not None else ""
            ),
            reject_tool_call=rejection,
            reject_reason=(
                str(payload.get("reject_reason") or "").strip()[:500]
                if rejection is not None else ""
            ),
        )


def classify_intent(content: str) -> tuple[str, str]:
    """Infer the intent and strip any legacy slash command.

    The legacy `/search`, `/image`, `/video`, `/research` prefixes predate
    slash commands: they carry no arguments and are stripped here so the
    question reaches the turn clean. Anything else starting with `/` is left
    intact for `_resolve_command`, which owns parsing, validation and the
    400-quality errors — a silently un-run command reads as one that ran.
    """
    text = content.strip()
    command, _, remainder = text.partition(" ")
    if (intent := _SLASH_COMMANDS.get(command.lower())) and remainder.strip():
        # A bare legacy prefix with no known command behind it (`/research`
        # alone, or `/search` with nothing after) is not a command line: the
        # command registry owns those words now, and stripping the prefix
        # would hide a typo (`/reserach x`) as a plain chat message.
        from chat.commands.registry import get as _get_command

        if _get_command(command.lower()) is None:
            return intent, remainder.strip()
        return "chat", text

    lowered = text.lower()
    # Recall wins over the search openers, which overlap badly with it: "what is
    # my name" starts with "what is" but is a question about this conversation,
    # not the world. Getting this wrong now costs a real web search, because an
    # explicit search intent is seeded rather than left to the model.
    if any(lowered.startswith(opener) for opener in _SEARCH_OPENERS):
        return ("chat" if looks_like_recall(text) else "search"), text
    return "chat", text


def looks_like_recall(content: str) -> bool:
    """Whether the user is asking about something said earlier in this chat."""
    lowered = (content or "").lower()
    return any(marker in lowered for marker in _RECALL_MARKERS)


# ── Slash commands (P10, §18) ────────────────────────────────────────────

@dataclass(slots=True)
class CommandResolution:
    """What resolving a command line decided, before the turn runs."""

    command: Any = None
    args: dict[str, Any] = field(default_factory=dict)
    #: The free text left after the command's arguments (the task, the goal).
    text: str = ""
    #: Trailing context block for this turn (never the system prompt).
    context_block: str = ""
    intent: str = ""
    #: Tool names to pin for this turn, or None to leave the toolbox alone.
    tool_pin: tuple[str, ...] | None = None
    #: A run to start through the one door before the chat model runs.
    start: dict[str, Any] = field(default_factory=dict)
    #: Stored on the user message so regenerate replays the same call.
    command_json: dict[str, Any] | None = None
    #: A rewritten question for the transcript (the chip text stays short).
    question: str | None = None


async def _resolve_command(*, session, user, request, intent: str,
                           question: str) -> CommandResolution:
    """Resolve `TurnRequest.command` or a typed `/line` into a turn plan.

    Raises `TurnError` for anything that fails to parse or resolve — shown
    under the input with the text left in place, never sent to the model as
    plain text, because a silently un-run command reads as one that ran.
    """
    from chat.commands import registry as command_registry
    from chat.commands.resolve import (
        CommandError,
        resolve_arg_value,
        resolve_line,
        split_leading_command,
    )
    from chat.commands.resolve import _requires_met

    autonomy = (getattr(session, "autonomy", "") or "ask").strip().lower()
    ctx_kwargs = dict(user_id=user.id, session_id=str(session.id),
                      autonomy=autonomy, guest=False)
    from chat.commands.registry import CommandContext

    ctx = CommandContext(**ctx_kwargs)
    payload = request.command
    try:
        if payload is not None:
            name = str(payload.get("name") or "").strip().lower().lstrip("/")
            raw_args = payload.get("args") or {}
            text = str(payload.get("text") or "")
            cmd = command_registry.get(name)
            if cmd is None:
                raise CommandError(f"No command called '/{name}'. Try /help.")
            if not _requires_met(cmd, ctx)[0]:
                raise CommandError(f"/{cmd.name} is not available right now.")
            args: dict[str, Any] = {}
            text_args = [a for a in cmd.args if a.kind == "text"]
            first_text = text_args[0] if text_args else None
            for arg in cmd.args:
                if arg.name in raw_args and str(raw_args[arg.name] or "").strip():
                    args[arg.name] = await resolve_arg_value(
                        arg, str(raw_args[arg.name]), ctx)
                elif arg.name in (raw_args or {}):
                    args[arg.name] = raw_args[arg.name]
                elif arg.kind == "text" and arg is first_text and text.strip():
                    # The trailing free text fills the first text argument —
                    # required or not — mirroring `resolve_line`.
                    args[arg.name] = text.strip()
                    text = ""
                elif arg.required and arg.name not in args:
                    # An empty trailing text is not a refusal for a text
                    # argument: the handler asks for it. Required *entity*
                    # arguments already refused above.
                    if arg.kind == "text":
                        continue
                    raise CommandError(f"'{arg.name}' is required. {arg.hint}".strip())
                elif arg.default is not None:
                    args[arg.name] = arg.default
        else:
            # A typed `/line` sent without the palette: parsed by the backend.
            # `question` is the classify_intent-stripped text (`/research`
            # arrives as the bare query), so re-resolve from the raw content.
            cmd, args, text = await resolve_line(request.content, ctx)
    except CommandError as exc:
        raise TurnError(str(exc)) from exc

    from chat.commands.registry import CommandCall

    try:
        result = await cmd.handler(CommandCall(name=cmd.name, args=args, text=text), ctx)
    except Exception:  # noqa: BLE001
        logger.exception("[Commands] /%s handler failed", cmd.name)
        raise TurnError(f"/{cmd.name} could not run.") from None
    if result.status == "error":
        raise TurnError(result.message or f"/{cmd.name} failed.")
    if result.status == "confirm":
        # A command whose first action spends money or leaves the platform
        # does nothing until confirmed — the confirm sheet is the approval.
        names = {"goal": "a mission", "schedule": "a schedule",
                 "publish": "a published page", "code-review": "a review"}
        raise TurnError(
            (result.message or f"/{cmd.name} needs confirmation.")
            + f" Open /{cmd.name} from the command menu to review "
              f"{names.get(cmd.name, 'it')} before it starts."
        )
    resolution = CommandResolution(
        command=cmd, args=dict(result.args or args), text=text,
        context_block=result.context_block or "",
        intent=result.intent or "", tool_pin=result.tool_pin,
        start=dict(result.start or {}),
        command_json={"name": cmd.name, "args": dict(result.args or args)},
        question=question,
    )
    if result.intent:
        resolution.intent = result.intent
    # Client-kind commands never reach the turn: they run in the browser.
    if cmd.kind == "client":
        raise TurnError(
            f"/{cmd.name} runs in the app itself — pick it from the command menu.")
    return resolution


async def _command_toolbox(resolution: CommandResolution | None, *, user,
                           session, file_scope) -> dict[str, Any]:
    """Narrow the turn's toolbox when a command pins tools (`/deck`, ...)."""
    if resolution is None or not resolution.tool_pin:
        return {}
    from chat.turn import reviewer as _reviewer

    pinned = frozenset(resolution.tool_pin)

    async def _source():
        source = _reviewer.read_only_source(
            user_id=user.id, memory_enabled=session.memory_enabled,
            session_key=str(session.id), file_scope=file_scope,
        ) if (getattr(session, "autonomy", "") or "ask") == "plan" else None
        if source is not None:
            offered = await source()
        else:
            from chat.tools import get_available_tools

            offered = await get_available_tools(
                user.id, memory_enabled=session.memory_enabled,
                session_key=str(session.id), file_scope=file_scope,
            )
        names = {t["function"]["name"] for t in offered}
        keep = (pinned & names) | {
            "get_current_time", "update_todos", "render_chart",
            "render_dashboard", "notify_user",
        }
        return [t for t in offered if t["function"]["name"] in keep]

    return {"tool_source": _source}


async def _start_command_run(resolution: CommandResolution, *, session, user,
                             sink, metadata: dict[str, Any]) -> None:
    """Start a `/agent` delegation through the one door, before the chat turn.

    The typed command is the consent for the first action (like the Run
    button); every tool call the started agent then makes is gated as usual.
    The run card streams as `agent_run` frames so the transcript shows status,
    todos, files and a link to `/runs/:id`, and the answer lands back as a
    message attributed to the agent.
    """
    start = resolution.start or {}
    if start.get("type") != "agent_run":
        return
    from chat.commands.agents import run_agent_start

    try:
        info = await run_agent_start(user=user, start=start,
                                     session_id=str(session.id))
    except Exception as exc:  # noqa: BLE001
        logger.warning("[Commands] /agent start failed: %s", exc)
        raise TurnError(f"The agent could not be started: {exc}") from None
    metadata["agent_run"] = {
        "agent_id": info["agent_id"], "agent_name": info["agent_name"],
        "execution_id": info["execution_id"],
    }
    await sink(Event.STATUS, {
        "phase": "agent_run", "message": f'{info["agent_name"]} is working...',
        "agent_run": metadata["agent_run"],
    })


# ── Outcome ──────────────────────────────────────────────────────────────────

@dataclass(slots=True)
class TurnOutcome:
    user_message: ChatMessage
    assistant_message: ChatMessage


# ── Pipeline steps ───────────────────────────────────────────────────────────

async def _guard_media_intent(intent: str, model: str) -> None:
    """Fail fast when the user asked for media the selected model cannot make."""
    requirement = _MEDIA_CAPABILITY.get(intent)
    if requirement is None:
        return

    from llm.models import AIModel

    flag, label = requirement
    entry = await AIModel.objects.filter(value=model, is_active=True).afirst()
    if entry is None or not getattr(entry, flag, False):
        raise TurnError(
            f"**{model}** does not support {label}. Switch to a model that does "
            f"(for example Grok Imagine) and try again."
        )


def _parse_effort(raw: Any) -> str | None:
    """Read `llm_effort` from a request body.

    Three answers, and the middle one is the reason this is not just
    `normalize`. `None` means the client said nothing, so the session's stored
    level stands — which is how a client that predates this field keeps
    working. `""` means the client explicitly asked for the model's own
    default, and must be able to *clear* a stored level. A level name is
    itself. Anything else is a typo and reads as "said nothing", because
    failing a whole turn over an unrecognised preference is worse than
    answering at the level already chosen.
    """
    if not isinstance(raw, str):
        return None
    return "" if not raw.strip() else normalize_effort(raw)


async def _sync_model_choice(
    session: ChatSession, request: TurnRequest, provider: str, model: str,
    effort: str,
) -> None:
    """Persist a per-message model override as the session's new default.

    Effort is part of the same choice and is written by the same rule, but note
    the guard: a request that names *only* an effort still counts as an
    override. Requiring a model alongside it would mean a user who changes
    nothing but how hard the model thinks has that choice discarded on the next
    reload — the exact bug this function's model half already fixed once.
    """
    # `is not None`, not truthiness: `""` is the client explicitly asking for
    # the model's own default, and it has to be able to clear a stored level.
    # Reading it as "said nothing" makes the knob one-way.
    if not (request.provider or request.model or request.effort is not None):
        return
    current = (session.llm_provider, session.llm_model, session.llm_effort)
    if (provider, model, effort) == current:
        return
    session.llm_provider, session.llm_model, session.llm_effort = (
        provider, model, effort,
    )
    await session.asave(
        update_fields=["llm_provider", "llm_model", "llm_effort"]
    )


async def _recall_block(session: ChatSession, question: str) -> str:
    """
    Look up earlier turns when the user is plainly referring to them.

    The agent has `search_conversation_history` and the prompt tells it to use
    it, but measured against smaller models it often simply did not — answering
    "I don't have that in my context" with an empty tool trace while the same
    query through the tool returned the answer immediately. Recall is the
    feature, so it cannot depend on a given model's willingness to call a
    function. The tool remains available for what this misses.
    """
    if not (session.memory_enabled and looks_like_recall(question)):
        return ""

    from chat import tools as tool_registry

    try:
        raw = await tool_registry.execute_tool(
            "search_conversation_history",
            {"query": question},
            {"user_id": session.user_id, "session_id": str(session.id)},
        )
        matches = json.loads(raw).get("matches") or []
    except Exception:
        logger.warning("[Recall] Eager history search failed", exc_info=True)
        return ""

    if not matches:
        return ""

    lines = "\n".join(
        f"- [{m['role']} @ {m['timestamp'][:19]}] {m['snippet']}" for m in matches
    )
    return (
        "\n\n[RECALLED FROM EARLIER IN THIS CONVERSATION — these turns are outside "
        "your visible window but did happen. Use them to answer; do not tell the "
        f"user you have no record of them.]\n{lines}"
    )


#: Intent → the tool that intent *is*, and the argument carrying the question.
#: Choosing `/search` or `/research` is the user stating the tool should run, so
#: it runs. Leaving it to the model would mean an explicit search request could
#: come back with no sources because the model decided it knew the answer.
_INTENT_SEED_TOOL = {
    "search": ("web_search", "query"),
    "research": ("deep_research", "topic"),
}


async def _seed_intent_tool(
    intent: str, question: str, turn: TurnContext, metadata: dict[str, Any]
) -> tuple[str, list[dict[str, Any]]]:
    """Run the tool an explicit intent implies, before the model's first turn."""
    seed = _INTENT_SEED_TOOL.get(intent)
    if seed is None:
        return "", []

    tool_name, argument = seed
    await turn.sink(Event.STATUS, {
        "phase": "searching",
        "message": "Searching the web..." if intent == "search" else "Researching...",
    })

    output, trace = await agent.run_tool_eagerly(
        tool_name, {argument: question}, turn=turn, metadata=metadata
    )

    try:
        text = json.loads(output).get("text") or output
    except (json.JSONDecodeError, TypeError, AttributeError):
        text = output

    await turn.sink(Event.STATUS, {
        "phase": "analyzing", "message": "Synthesising results...",
    })
    return f"\n\n[RESULTS FROM {tool_name.upper()}]\n{text}", [trace]


def _now_string(prefs=None) -> str:
    """Current time where the user is, for the per-turn context update.

    Was the server's zone (UTC in every deployment), so "what time is it" and
    "remind me tomorrow morning" were answered five and a half hours out for a
    user in India while their own Settings said Asia/Kolkata.
    """
    from core.preferences import DEFAULTS, local_now

    return local_now(prefs or DEFAULTS)


async def _user_memory_block(user_id: int | None, prefs=None) -> str:
    """What we know about this user, for the system prompt, or ''.

    Two sources, both session-stable: what they told us in Settings, then what
    the assistant has remembered about them. Settings comes first because it is
    what they chose to say about themselves.

    Degrades to nothing rather than failing the turn, like every other context
    gatherer here: a memory store that cannot be read should cost the answer
    its personalisation, not its existence.
    """
    from core.preferences import about_user

    about = about_user(prefs) if prefs is not None else ""
    try:
        from core.memory import for_prompt

        memory = await sync_to_async(for_prompt)(user_id)
    except Exception:  # noqa: BLE001
        logger.warning("[Chat] Could not read user memory for %s", user_id,
                       exc_info=True)
        memory = ""
    return "\n\n".join(block for block in (about, memory) if block)


async def _chat_file_scope(user):
    """The slice of the user's document tree this chat turn may address.

    Fixed rather than configured, unlike an agent's: an agent is built once and
    run unattended, so which files it may touch is a decision worth making per
    agent, while chat is the user's own hands on their own tree. They get the
    whole thing to read and `/Chat/` to write into.

    Degrades to `None` rather than failing the turn, which is the same rule
    `build_file_scope` follows for agents and `descriptors` follows for MCP: a
    tree that cannot be reached should cost the conversation its file tools,
    not its answer. Creating the folder is a write, hence the round trip.
    """
    try:
        from inference.vfs import chat_scope

        return await sync_to_async(chat_scope)(user)
    except Exception:  # noqa: BLE001
        logger.warning("[Chat] Could not build a file scope for user %s",
                       getattr(user, "id", None), exc_info=True)
        return None


def _thread_id(session: ChatSession) -> str:
    """
    Checkpointer key for this turn.

    With memory off the id is deliberately throwaway. The graph keeps its own
    checkpointed message list keyed by thread_id — a second, independent copy of
    the conversation — so emptying the history payload and withholding the
    search tool would not stop the model recalling earlier turns from the
    checkpoint, and the toggle would appear to do nothing.
    """
    if session.memory_enabled:
        return str(session.id)
    return f"{session.id}:nomem:{uuid4()}"


def context_summary(answer: str) -> str:
    """
    Condense a long answer for later context windows.

    Prefers an explicit conclusion section when the answer has one, because that
    is the part a later turn actually needs; otherwise falls back to the opening.
    """
    conclusion = re.search(
        r"(?is)\n(?:#{1,3}|\*\*)\s*(?:bottom line|conclusion|summary|final takeaway)"
        r"[:\s\n]+(.*?)$",
        answer,
    )
    source = answer
    if conclusion and len(conclusion.group(1).split()) >= 10:
        source = f"[Key points]: {conclusion.group(1).strip()}"

    plain = re.sub(r"[*#_`|\[\]]", "", source)
    plain = re.sub(r"-{3,}", " ", plain)
    words = re.sub(r"\s+", " ", plain).strip().split()

    if len(words) <= 130:
        return " ".join(words)
    return " ".join(words[:130]) + "... [preview]"


def _message_type(intent: str, metadata: Mapping[str, Any]) -> str:
    if "workflow_id" in metadata:
        return "workflow_suggestion"
    if intent in ("image", "video"):
        return intent
    if metadata.get("sources"):
        return "search"
    return "chat"


async def persist_interrupted_answer(
    session: ChatSession, user, text: str
) -> ChatMessage | None:
    """
    Store what a stopped turn had streamed before the user stopped it.

    The normal path writes the reply only once the agent returns, so a turn
    cancelled part-way would leave the user's question with no answer at all.
    Whatever reached the client is kept instead, flagged so the UI can mark it
    as incomplete. Nothing is written when nothing was streamed.
    """
    if not text.strip():
        return None

    message = await ChatMessage.objects.acreate(
        session=session,
        role="assistant",
        content=text,
        message_type="chat",
        metadata={"interrupted": True, "model": session.llm_model},
    )
    await _notify(user, session, message)
    return message


async def _notify(user, session: ChatSession, message: ChatMessage) -> None:
    try:
        from notifications.utils import create_notification

        await sync_to_async(create_notification)(
            user=user,
            type="new_message",
            title="New AI response",
            message=f'Assistant replied in "{session.title}"',
            data={"session_id": str(session.id), "message_id": message.id},
        )
    except Exception:
        logger.warning("[Notify] Could not create message notification", exc_info=True)


# ── The turn ─────────────────────────────────────────────────────────────────


class _PhaseTimer:
    """Wall clock for the work a turn does *before* the model is called.

    `agent._log_latency` starts inside `agent_node`, so everything here — the
    preflight, the user-message write, history, memory, attachments, the file
    scope, recall and intent seeding — was invisible to the only latency
    instrument the project had. That is 15-20 sequential awaits on the path to
    the first token, and a number nobody can see is a number nobody optimises.

    Deliberately one line and only non-zero phases, the same contract
    `_log_latency` keeps: it runs on every turn, so it has to be cheap to emit
    and cheap to grep.
    """

    __slots__ = ("marks", "_t0", "_last")

    def __init__(self) -> None:
        self.marks: dict[str, int] = {}
        self._t0 = self._last = time.monotonic()

    def mark(self, phase: str) -> None:
        now = time.monotonic()
        self.marks[phase] = int((now - self._last) * 1000)
        self._last = now

    @property
    def total_ms(self) -> int:
        return int((time.monotonic() - self._t0) * 1000)

    def log(self, intent: str) -> None:
        detail = " ".join(f"{k}={v}ms" for k, v in self.marks.items() if v)
        logger.info(
            "[Latency] pre-model total=%dms intent=%s %s",
            self.total_ms, intent, detail,
        )


async def run_chat_turn(
    *,
    session: ChatSession,
    user,
    request: TurnRequest,
    sink: EventSink = null_sink,
) -> TurnOutcome:
    """
    Run one turn: persist the user's message, run the agent, persist the reply.

    Raises `TurnError` for conditions the user should be told about directly
    (unusable model for the requested media, no credential or credit for the
    chosen provider, empty request) — all checked before any status is
    streamed, so the client never shows work that cannot happen.
    """
    if getattr(request, 'regenerate_of', None):
        try:
            from logs.signals_api import arecord_signal

            await arecord_signal(user.id, 'regenerated', session_id=str(session.id),
                                 message_id=request.regenerate_of,
                                 detail={'of_message_id': request.regenerate_of})
        except Exception:  # noqa: BLE001
            pass
    provider = request.provider or session.llm_provider
    model = request.model or session.llm_model
    # `or` rather than a None check on purpose: the client sends "" to mean
    # "back to the model's default", and that has to be able to clear a stored
    # level rather than being read as "said nothing".
    effort = request.effort if request.effort is not None else session.llm_effort
    intent, question = (
        (request.intent, request.content) if request.intent
        else classify_intent(request.content)
    )

    # ── Slash command ──
    # Resolved here (before `llm.preflight()`), applied after: `intent` and
    # `question` are needed from the top — the chip text is the question —
    # while the expansion rides the trailing context message built below
    # (§18.7), never the system prompt (the clock trap), so the baseline
    # stays byte-identical to a turn without one (prefix-cache guard). A
    # command that cannot be paid for still raises at preflight below, so a
    # command never makes a turn look busy before it is known to be payable.
    command_resolution = None
    if request.command is not None or question.lstrip().startswith("/"):
        command_resolution = await _resolve_command(
            session=session, user=user, request=request,
            intent=intent, question=question,
        )
        if command_resolution.intent:
            intent = command_resolution.intent
        if command_resolution.question is not None:
            question = command_resolution.question

    phases = _PhaseTimer()
    # Model calls made inside tools on other models (the vision witness) are
    # collected here and priced into this turn — see `side_calls`.
    from . import side_calls

    side_calls.start()

    await _guard_media_intent(intent, model)
    phases.mark("media_guard")

    # Before anything is persisted or streamed. A turn with no credential
    # behind it used to get all the way to the model call — the client had
    # already been shown "Starting up..." and then "Processing your message..."
    # — and the failure came back dressed as the assistant apologising. There
    # is nothing to think about if the call cannot be made, so the user is told
    # at once, while the composer still holds their message to resend.
    try:
        await llm.preflight(provider=provider, model=model, user_id=user.id)
    except llm.LLMUnavailable as exc:
        raise TurnError(str(exc)) from exc
    phases.mark("preflight")

    await _sync_model_choice(session, request, provider, model, effort)
    phases.mark("sync_choice")

    thread_id = _thread_id(session)
    if request.approve_tool_call:
        await agent.approve_tool_call(
            thread_id, request.approve_tool_call,
            remember=request.remember_approval,
            scope=request.approval_scope,
            # Not `thread_id`: with memory off that is a throwaway id and the
            # session-scoped allowance would be filed where nothing looks.
            session_key=str(session.id),
            user_id=user.id,
        )
    elif request.reject_tool_call:
        # Not fatal when there is no state for the thread: the user has already
        # dismissed the card, and telling them their refusal failed leaves them
        # nothing to do about it. The turn goes on and the model simply never
        # sees the call it asked for.
        await agent.reject_tool_call(
            thread_id, request.reject_tool_call, reason=request.reject_reason,
            user_id=user.id,
        )

    await sink(Event.STATUS, {"phase": "planning", "message": "Starting up..."})

    # An approval or a refusal carries no new text, so it resumes against the
    # message that triggered the pause rather than inventing a new turn.
    # The slash command rides on the user message (the transcript chip), and
    # on the assistant message via `_persist_answer(..., command_json=...)` —
    # regenerate replays the same call from either row. `dict(...)` because
    # the ORM keeps the very object it is given: handing it the resolution's
    # own dict lets a later mutation rewrite the stored row.
    _command_json: dict[str, Any] | None = (
        dict(command_resolution.command_json)
        if command_resolution is not None
        and command_resolution.command_json else None
    )
    user_message = (
        await ChatMessage.objects.filter(session=session, role="user").alast()
        if (request.approve_tool_call or request.reject_tool_call) else None
    ) or await ChatMessage.objects.acreate(
        session=session, role="user",
        content=question or "[Approved tool call]", message_type="chat",
        metadata={"command": _command_json} if _command_json else {},
    )
    phases.mark("user_message")

    await sink(Event.STATUS, {
        "phase": "thinking",
        "message": "Processing your message...",
        "user_message_id": user_message.id,
    })

    from llm.models import AIModel

    model_entry = await AIModel.objects.filter(value=model, is_active=True).afirst()
    supports_docs = bool(model_entry and model_entry.supports_document_input)
    phases.mark("model_entry")

    # Memory off answers from this message alone. Nothing is deleted — the turns
    # stay in the DB and return the moment it is switched back on.
    past: list[ChatMessage] = []
    wire_history: list[dict[str, str]] = []
    if session.memory_enabled:
        past = await history.load_history(
            session, exclude_id=user_message.id, supports_docs=supports_docs
        )
        wire_history = history.to_wire_history(
            past, max_tokens=MAX_CONTEXT_TOKENS - 4_000
        )
    else:
        await sink(Event.STATUS, {
            "phase": "memory_off",
            "message": "Memory is off — answering from this message only.",
        })
    phases.mark("history")

    # Read once per turn and folded into the baseline, not the per-turn update:
    # it is standing knowledge about the person, and it changes only when a
    # fact is written. See `build_system_message`.
    from core.preferences import for_user as preferences_for

    prefs = await sync_to_async(preferences_for)(user.id)
    system_message = prompts.build_system_message(
        session, user_memory=await _user_memory_block(user.id, prefs),
    )
    phases.mark("user_memory")

    # ── Attachments ──
    candidates = await history.recent_attachments(past)
    # Resolved once per turn rather than per attachment: it is a profile read
    # plus a credential lookup, and the answer cannot change mid-turn.
    witness = await vision.witness_available(user.id)
    phases.mark("vision_witness")
    sendable, blocked = await history.partition_attachments(
        candidates, model=model, witness=witness
    )
    metadata: dict[str, Any] = {
        "intent": intent, "model": model, "provider": provider, "effort": effort,
    }

    blocked_notice = ""
    if blocked:
        notice = history.describe_blocked(blocked)
        logger.info("[Attachments] Withheld %d from %s (witness=%s)",
                    len(blocked), model, witness)
        await sink(Event.ATTACHMENTS_BLOCKED, {"message": notice, "items": blocked})
        metadata["blocked_attachments"] = blocked
        # The model gets its own version, carrying the ids: an agent that knows
        # a file exists but not its id can only apologise for not seeing it.
        blocked_notice = history.describe_for_model(blocked)

    # Everything that changes turn to turn — the clock, this turn's mode nudge,
    # the files this model cannot be shown — rides as a
    # trailing `system` message instead of being concatenated onto the baseline.
    # Folded in, the clock alone made the cached prefix differ on every turn.
    context_update = prompts.build_context_update(
        session, _now_string(prefs), intent, blocked_notice=blocked_notice
    )
    if command_resolution is not None and command_resolution.context_block:
        context_update = (
            f"{context_update}\n\n{command_resolution.context_block}"
            if context_update else command_resolution.context_block
        )
    if context_update:
        wire_history.append({"role": "system", "content": context_update})

    attachments, extracted_text = await agent.prepare_attachments(
        sendable, model=model, provider=provider, user_id=user.id
    )
    phases.mark("attachments")

    # ── Prompt ──
    prompt = question
    if request.reference_message_id is not None:
        prompt = (
            f"[The user's question refers to part of message "
            f"{request.reference_message_id}; prioritise that context.]\n\n{prompt}"
        )
    prompt += await _recall_block(session, question)
    phases.mark("recall")
    prompt += extracted_text

    file_scope = await _chat_file_scope(user)
    phases.mark("file_scope")

    # ── Autonomy ──
    # `ask` is today's behaviour. `auto` lets the reviewer downgrade clear
    # matches; `plan` withholds every mutating tool. Mid-turn switches arrive
    # through the steer mailbox against `approval_modes`; `plan` is not
    # switchable because the toolbox is already built.
    from chat.tools import permissions as _permissions
    from chat.tools import schemas as _schemas
    from chat.tools.registry import sensitive_names as _sensitive_names
    from chat.turn import reviewer as _reviewer

    session_autonomy = (getattr(session, 'autonomy', '') or 'ask').strip().lower()
    if session_autonomy not in ('ask', 'auto', 'plan', 'review'):
        session_autonomy = 'ask'
    _auto_policy = _reviewer.auto_policy(
        user_text=question, provider=provider, model=model,
    )

    turn = TurnContext(
        file_scope=file_scope,
        provider=provider,
        model=model,
        system_message=system_message,
        user_id=user.id,
        session_id=str(session.id),
        intent=intent,
        user_text=question,
        history=tuple(wire_history),
        attachments=attachments,
        memory_enabled=session.memory_enabled,
        max_iterations=agent.iteration_limit(intent),
        effort=effort or None,
        sink=sink,
        **({'tool_source': _reviewer.read_only_source(
            user_id=user.id, memory_enabled=session.memory_enabled,
            session_key=str(session.id), file_scope=file_scope,
        )} if session_autonomy == 'plan' else {}),
        **({'approval_policy': _auto_policy} if session_autonomy == 'auto' else {}),
        **((await _command_toolbox(
            command_resolution, user=user, session=session,
            file_scope=file_scope,
        )) if command_resolution is not None else {}),
        approval_modes={
            'ask': (frozenset(_sensitive_names()), _permissions.default_policy),
            'review': (frozenset(
                t['function']['name'] for t in _schemas()
            ), _permissions.default_policy),
            'auto': (frozenset(), _auto_policy),
        },
    )

    # A command that starts a run directly (`/agent`): the typed command is
    # the consent for the first action, so it starts here — through the one
    # door — before the chat model ever runs. The chat turn still runs after,
    # and the run card + the agent's answer land in the same transcript.
    if command_resolution is not None and command_resolution.start:
        await _start_command_run(
            command_resolution, session=session, user=user, sink=sink,
            metadata=metadata,
        )

    seed_text, seed_trace = await _seed_intent_tool(intent, question, turn, metadata)
    phases.mark("intent_seed")
    phases.log(intent)

    turn_started = time.monotonic()
    try:
        result = await agent.run_turn(
            turn,
            prompt=prompt + seed_text,
            thread_id=thread_id,
            metadata=metadata,
            tool_trace=seed_trace,
        )
    except llm.LLMUserActionable as exc:
        # Credit can run out mid-turn, and a model can reach end of life between
        # one turn and the next — the preflight above only proves a key exists,
        # not that it can still pay or that the model is still served. Same
        # treatment either way: report it as an error, never as the assistant's
        # reply.
        logger.warning("[Turn] Provider error for user %s: %s", user.id, exc)
        raise TurnError(str(exc)) from exc
    turn_elapsed_s = time.monotonic() - turn_started

    assistant_message = await _persist_answer(
        session=session, user=user, turn=turn, result=result,
        question=question, intent=intent, elapsed_s=turn_elapsed_s,
        command_json=(
            command_resolution.command_json
            if command_resolution is not None else None
        ),
    )
    await _notify(user, session, assistant_message)

    return TurnOutcome(user_message=user_message, assistant_message=assistant_message)


_EMPTY_ANSWER_FALLBACK = (
    "I wasn't able to produce an answer for that. Please try rephrasing, or try "
    "again in a moment."
)


def _combine_payers(so_far: str, this_turn: str) -> str:
    """The conversation's payer after one more turn.

    The first turn sets it; a later turn that disagrees makes it `mixed` for
    good, the way one unpriced turn makes a total unpriced: after a switch
    the sum is part one person's bill and part credits, and no single label is
    true of it. An unknown turn leaves what is known alone.
    """
    if not this_turn:
        return so_far
    if not so_far or so_far == this_turn:
        return this_turn
    return "mixed"


@sync_to_async
def _price_turn(model_id: str, usage):
    """What this turn cost, and how much we trust the figure.

    Wrapped in `sync_to_async` because pricing reads the model registry, and
    guarded because a cost is telemetry about an answer the user already has:
    a registry hiccup must cost the number, never the message.
    """
    from llm.pricing import cost_for_usage

    try:
        return cost_for_usage(model_id or "", usage)
    except Exception:  # noqa: BLE001
        logger.exception("[Turn] Pricing failed for %s", model_id)
        return Decimal("0"), "unpriced"

async def _persist_answer(
    *,
    session: ChatSession,
    user,
    turn: TurnContext,
    result: TurnResult,
    question: str,
    intent: str,
    elapsed_s: float = 0,
    command_json: dict[str, Any] | None = None,
) -> ChatMessage:
    """Store the assistant's reply and everything the UI needs alongside it."""
    answer = _INTERNAL_TAGS.sub("", result.answer or "").strip()

    if not answer:
        logger.warning("[Turn] Empty answer from %s/%s", turn.provider, turn.model)
        answer = _EMPTY_ANSWER_FALLBACK
        if sources := result.metadata.get("sources"):
            links = "\n".join(
                f"- [{s.get('title', 'Source')}]({s.get('url', '#')})"
                for s in sources[:5]
            )
            answer += f"\n\nSources gathered so far:\n{links}"

    metadata = dict(result.metadata)
    metadata.update(
        tokens=result.tokens,
        thinking=result.thinking,
        tool_trace=result.tool_trace,
    )
    if result.awaiting_approval:
        metadata["awaiting_approval"] = True
    # The slash command rides here (not through the graph): regenerate replays
    # the same call, and the transcript chip reads it. The graph deliberately
    # never sees it — working metadata is graph state, and every node returns
    # it untouched-or-not at all (`test_todos.py` pins that nodes rewrite
    # `messages` and nothing else).
    if command_json is not None:
        metadata["command"] = command_json
    # Cards survive the turn the same way: a reload replays them from the
    # message rather than re-running anything.
    for key in ("agent_run",):
        if key in result.metadata and key not in metadata:
            metadata[key] = result.metadata[key]

    # Every model call this turn made on the user's model, not only the answer:
    # the conversation's cost is shown in the header, and a figure that leaves
    # out a call per answer is understated while looking exact.
    extra_usage: list = []
    if not result.awaiting_approval and elapsed_s <= FOLLOW_UPS_SLOW_TURN_SECONDS:
        metadata["follow_ups"] = await agent.suggest_follow_ups(
            turn, question=question, answer=answer, usage_sink=extra_usage,
        )
    elif not result.awaiting_approval:
        # The turn already ran long; a further LLM call would delay the persist
        # for three questions nobody asked for. The answer is kept as is.
        logger.info(
            "[Turn] Skipping follow-ups after a %.0fs turn (over %ds)",
            elapsed_s, FOLLOW_UPS_SLOW_TURN_SECONDS,
        )
        metadata["follow_ups"] = []

    if len(answer.split()) > ASSISTANT_SUMMARY_WORD_LIMIT:
        metadata["summary"] = context_summary(answer)

    # Priced against the model that actually answered, before the row is
    # written, so the message carries its own cost rather than having one
    # inferred later from a session-level rate that may since have changed.
    cost, cost_source = await _price_turn(session.llm_model, result.usage)
    extra_tokens = 0
    for usage in extra_usage:
        if usage.is_empty:
            continue
        # Priced on its own rather than folded into the answer's usage: the
        # answer may be billed and this estimated, and `combine_sources` is
        # what keeps that mix from being reported as billed.
        extra_cost, extra_source = await _price_turn(turn.model, usage)
        cost += extra_cost
        cost_source = combine_sources([cost_source, extra_source])
        extra_tokens += usage.total or usage.billable_total
    from . import side_calls

    # Tools that spend money themselves (a generated image). Recorded by the
    # tool's side effect; priced by the provider, not by our token table.
    for entry in (result.metadata or {}).get("tool_costs") or []:
        try:
            cost += Decimal(str(entry.get("cost_usd") or 0))
        except (ArithmeticError, ValueError):
            continue
        cost_source = combine_sources([cost_source, entry.get("cost_source") or "estimated"])

    for call in side_calls.collected():
        # Each on its own model: the witness is a different, cheaper model
        # from the one answering, and pricing it at the answer's rate would
        # be wrong in either direction.
        call_cost, call_source = await _price_turn(call.model, call.usage)
        cost += call_cost
        cost_source = combine_sources([cost_source, call_source])
        extra_tokens += call.usage.total or call.usage.billable_total

    # Whose money `cost` is. Resolved the way `preflight` resolved the key this
    # turn actually used, so the label cannot disagree with the call.
    paid_by = await llm.payer(
        provider=turn.provider, model=turn.model, user_id=turn.user_id,
    )

    message = await ChatMessage.objects.acreate(
        session=session,
        role="assistant",
        content=answer,
        message_type=_message_type(intent, metadata),
        metadata=metadata,
        model_id=session.llm_model or "",
        input_tokens=result.usage.input,
        output_tokens=result.usage.output,
        cached_read_tokens=result.usage.cached_read,
        cached_write_tokens=result.usage.cached_write,
        cost_usd=cost,
        cost_source=cost_source,
        paid_by=paid_by,
    )

    session.total_tokens_used += result.tokens + extra_tokens
    session.total_cost_usd = (session.total_cost_usd or Decimal("0")) + cost
    # Combined, not overwritten: one unpriced turn makes the conversation's
    # total unpriced for good, because from then on the sum is missing money
    # nobody can put back.
    session.cost_source = combine_sources([session.cost_source, cost_source])
    session.paid_by = _combine_payers(session.paid_by, paid_by)
    await session.asave(update_fields=[
        "total_tokens_used", "total_cost_usd", "cost_source", "paid_by",
    ])

    try:
        await persist_generated_media(user, session, message)
    except Exception:
        logger.exception("[Turn] Persisting generated media failed")

    return message


async def persist_generated_media(user, session: ChatSession, message: ChatMessage) -> None:
    """
    Save generated images/videos as a Document plus ChatAttachment.

    Text answers are deliberately not indexed here; `context_summary` handles
    context management for long replies.
    """
    metadata = message.metadata or {}
    media_url = metadata.get("media_url")
    if not media_url or message.message_type not in ("image", "video"):
        return

    import httpx
    from django.core.files.base import ContentFile

    from inference.models import Document
    from inference.utils import normalize_file_type

    from chat.models import ChatAttachment
    from core.safety.net import MAX_FETCH_BYTES, validate_url_async

    extension = ".png" if message.message_type == "image" else ".mp4"
    filename = f"generated_{uuid4().hex[:8]}{extension}"

    # `media_url` comes from the provider's generation response, not a human —
    # so it is a model-controlled URL and must clear the SSRF guard before we
    # fetch it. Inline `data:` payloads are decoded locally and never leave the
    # process; anything else must be an http(s) URL that resolves to a public
    # address, and redirects are refused rather than followed to a private one.
    if media_url.startswith("data:"):
        try:
            _, _, b64 = media_url.partition(",")
            content = base64.b64decode(b64)
        except Exception:
            logger.warning("[Media] Could not decode inline data URL")
            return
        if len(content) > MAX_FETCH_BYTES:
            logger.warning("[Media] Inline media exceeds size cap; skipping")
            return
    else:
        is_safe, reason = await validate_url_async(media_url)
        if not is_safe:
            logger.warning("[Media] Refused %s: %s", media_url, reason)
            return
        async with httpx.AsyncClient(follow_redirects=False) as client:
            response = await client.get(media_url, timeout=30)
        if response.status_code != 200:
            logger.warning("[Media] Could not download %s (HTTP %s)", media_url,
                           response.status_code)
            return
        content = response.content
        if len(content) > MAX_FETCH_BYTES:
            logger.warning("[Media] %s exceeds size cap (%d bytes); skipping",
                           media_url, len(content))
            return

    # `file_type` comes from the filename, not from `message.message_type`.
    # message_type is a message-*intent* vocabulary ('chat', 'search',
    # 'coding', 'workflow_suggestion', …) that is not Document's — it only
    # looked right because the two reachable intents here happen to be spelled
    # 'image' and 'video'. It is also `max_length=30` against Document's 10, so
    # any wider intent reaching this line would be a DataError on PostgreSQL.
    document = await sync_to_async(Document.objects.create)(
        user=user, name=filename, file_type=normalize_file_type(filename),
        file_size=len(content), status="pending",
    )

    @sync_to_async
    def _store() -> None:
        document.file.save(filename, ContentFile(content))
        document.save()

    await _store()

    attachment = await ChatAttachment.objects.acreate(
        session=session, message=message, filename=filename, file=document.file,
        file_type=message.message_type, file_size=document.file_size,
        inference_document=document,
    )
    message.metadata = {**metadata, "attachment_id": str(attachment.id)}
    await message.asave(update_fields=["metadata"])
    logger.info("[Media] Persisted %s as document %s", message.message_type, document.id)
