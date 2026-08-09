"""
One chat turn, end to end.

Both endpoints — streaming and plain — run exactly this. They differ only in the
`EventSink` they pass: an SSE bridge, or one that discards. That is the reason
this module exists; the two used to be separate 300- and 860-line
implementations of the same pipeline that had already drifted apart on
attachments, recall and error handling.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Mapping
from uuid import uuid4

from asgiref.sync import sync_to_async

from workflow_backend.thresholds import (
    ASSISTANT_SUMMARY_WORD_LIMIT,
    MAX_CONTEXT_TOKENS,
)

from . import agent, history, prompts
from .agent import TurnContext, TurnResult
from .events import Event, EventSink, null_sink
from .models import ChatMessage, ChatSession

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
    reference_message_id: int | None = None
    approve_tool_call: str | None = None

    @classmethod
    def parse(cls, payload: Mapping[str, Any]) -> TurnRequest:
        """Read a request body, rejecting anything unusable up front."""
        approval = (payload.get("approve_tool_call") or "").strip() or None
        content = (payload.get("content") or "").strip()
        if not content and not approval:
            raise TurnError("A message is required.")

        requested = (payload.get("intent") or "").strip().lower()
        reference = payload.get("reference")

        return cls(
            content=content,
            intent=requested if requested in SELECTABLE_INTENTS else None,
            provider=(payload.get("llm_provider") or "").strip() or None,
            model=(payload.get("llm_model") or "").strip() or None,
            reference_message_id=(
                reference.get("message_id") if isinstance(reference, dict) else None
            ),
            approve_tool_call=approval,
        )


def classify_intent(content: str) -> tuple[str, str]:
    """Infer the intent and strip any leading slash command."""
    text = content.strip()
    command, _, remainder = text.partition(" ")
    if (intent := _SLASH_COMMANDS.get(command.lower())) and remainder.strip():
        return intent, remainder.strip()

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

    from nodes.models import AIModel

    flag, label = requirement
    entry = await AIModel.objects.filter(value=model, is_active=True).afirst()
    if entry is None or not getattr(entry, flag, False):
        raise TurnError(
            f"**{model}** does not support {label}. Switch to a model that does "
            f"(for example Grok Imagine) and try again."
        )


async def _sync_model_choice(
    session: ChatSession, request: TurnRequest, provider: str, model: str
) -> None:
    """Persist a per-message model override as the session's new default."""
    if not (request.provider or request.model):
        return
    if (provider, model) == (session.llm_provider, session.llm_model):
        return
    session.llm_provider, session.llm_model = provider, model
    await session.asave(update_fields=["llm_provider", "llm_model"])


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

    import json

    from . import tools as tool_registry

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
        import json

        text = json.loads(output).get("text") or output
    except (json.JSONDecodeError, TypeError, AttributeError):
        text = output

    await turn.sink(Event.STATUS, {
        "phase": "analyzing", "message": "Synthesising results...",
    })
    return f"\n\n[RESULTS FROM {tool_name.upper()}]\n{text}", [trace]


def _now_string() -> str:
    """Current time, in the project's timezone, for the system prompt."""
    from django.utils import timezone

    return timezone.localtime().strftime("%A, %B %d, %Y %I:%M %p %Z")


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
    (unusable model for the requested media, empty request).
    """
    provider = request.provider or session.llm_provider
    model = request.model or session.llm_model
    intent, question = (
        (request.intent, request.content) if request.intent
        else classify_intent(request.content)
    )

    await _guard_media_intent(intent, model)
    await _sync_model_choice(session, request, provider, model)

    thread_id = _thread_id(session)
    if request.approve_tool_call:
        await agent.approve_tool_call(thread_id, request.approve_tool_call)

    await sink(Event.STATUS, {"phase": "planning", "message": "Starting up..."})

    # An approval carries no new text, so it resumes against the message that
    # triggered the pause rather than inventing a new turn.
    user_message = (
        await ChatMessage.objects.filter(session=session, role="user").alast()
        if request.approve_tool_call else None
    ) or await ChatMessage.objects.acreate(
        session=session, role="user",
        content=question or "[Approved tool call]", message_type="chat",
    )

    await sink(Event.STATUS, {
        "phase": "thinking",
        "message": "Processing your message...",
        "user_message_id": user_message.id,
    })

    from nodes.models import AIModel

    model_entry = await AIModel.objects.filter(value=model, is_active=True).afirst()
    supports_docs = bool(model_entry and model_entry.supports_document_input)

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

    system_message = prompts.build_system_message(session, _now_string(), intent)

    # ── Attachments ──
    candidates = await history.recent_attachments(past)
    sendable, blocked = await history.partition_attachments(candidates, model=model)
    metadata: dict[str, Any] = {"intent": intent, "model": model, "provider": provider}

    if blocked:
        notice = history.describe_blocked(blocked)
        logger.info("[Attachments] Withheld %d from %s", len(blocked), model)
        await sink(Event.ATTACHMENTS_BLOCKED, {"message": notice, "items": blocked})
        metadata["blocked_attachments"] = blocked
        # Tell the model too, so it does not answer as though it saw them.
        system_message += (
            f"\n\n[ATTACHMENTS WITHHELD: {len(blocked)} file(s) the user uploaded "
            f"were not given to you. {notice} Say so plainly rather than guessing.]"
        )

    attachments, extracted_text = await agent.prepare_attachments(
        sendable, model=model, provider=provider
    )

    # ── Prompt ──
    prompt = question
    if request.reference_message_id is not None:
        prompt = (
            f"[The user's question refers to part of message "
            f"{request.reference_message_id}; prioritise that context.]\n\n{prompt}"
        )
    prompt += await _recall_block(session, question)
    prompt += extracted_text

    turn = TurnContext(
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
        sink=sink,
    )

    seed_text, seed_trace = await _seed_intent_tool(intent, question, turn, metadata)

    result = await agent.run_turn(
        turn,
        prompt=prompt + seed_text,
        thread_id=thread_id,
        metadata=metadata,
        tool_trace=seed_trace,
    )

    assistant_message = await _persist_answer(
        session=session, user=user, turn=turn, result=result,
        question=question, intent=intent,
    )
    await _notify(user, session, assistant_message)

    return TurnOutcome(user_message=user_message, assistant_message=assistant_message)


_EMPTY_ANSWER_FALLBACK = (
    "I wasn't able to produce an answer for that. Please try rephrasing, or try "
    "again in a moment."
)


async def _persist_answer(
    *,
    session: ChatSession,
    user,
    turn: TurnContext,
    result: TurnResult,
    question: str,
    intent: str,
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

    if not result.awaiting_approval:
        metadata["follow_ups"] = await agent.suggest_follow_ups(
            turn, question=question, answer=answer
        )

    if len(answer.split()) > ASSISTANT_SUMMARY_WORD_LIMIT:
        metadata["summary"] = context_summary(answer)

    message = await ChatMessage.objects.acreate(
        session=session,
        role="assistant",
        content=answer,
        message_type=_message_type(intent, metadata),
        metadata=metadata,
    )

    session.total_tokens_used += result.tokens
    await session.asave(update_fields=["total_tokens_used"])

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

    from .models import ChatAttachment

    extension = ".png" if message.message_type == "image" else ".mp4"
    filename = f"generated_{uuid4().hex[:8]}{extension}"

    async with httpx.AsyncClient() as client:
        response = await client.get(media_url, timeout=30)
    if response.status_code != 200:
        logger.warning("[Media] Could not download %s (HTTP %s)", media_url,
                       response.status_code)
        return

    document = await sync_to_async(Document.objects.create)(
        user=user, name=filename, file_type=message.message_type,
        file_size=len(response.content), status="pending",
    )

    @sync_to_async
    def _store() -> None:
        document.file.save(filename, ContentFile(response.content))
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
