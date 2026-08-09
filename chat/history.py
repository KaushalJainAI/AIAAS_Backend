"""
Building a turn's context: prior messages, their attachments, and what the
selected model is actually able to read.

Kept apart from the views because none of it is HTTP — it is the answer to
"what does the model get to see this turn", which the streaming endpoint, the
plain endpoint and any test all need identically.
"""
from __future__ import annotations

import logging
from typing import Any, Iterable, Sequence
from uuid import UUID

from asgiref.sync import sync_to_async

from workflow_backend.thresholds import (
    HISTORY_WINDOW,
    LARGE_FILE_PREVIEW_LENGTH,
    MAX_CONTEXT_TOKENS,
)

from .llm import estimate_tokens
from .models import ChatAttachment, ChatMessage, ChatSession

logger = logging.getLogger(__name__)

#: Attachment kind → the AIModel capability flag required to send it.
#: pdf/pptx/text are absent on purpose: they are read as extracted text, so they
#: ride in as ordinary tokens and need no special support.
_REQUIRED_CAPABILITY = {
    "image": "supports_image_input",
    "video": "supports_video_input",
}
_TEXT_EXTRACTED_TYPES = frozenset({"pdf", "pptx", "text"})

#: How many past turns may still contribute their uploaded files.
_ATTACHMENT_LOOKBACK = 5


# ── Loading history ──────────────────────────────────────────────────────────

def _enrich_with_attachments(
    messages: Sequence[ChatMessage], *, supports_docs: bool
) -> None:
    """Fold attachment text into the messages that reference it, in place."""
    # Whether an assistant turn has already responded *after* a given upload
    # decides how much of it to re-inject: the first time the model needs the
    # full text, later it only needs enough to know the file exists and can be
    # re-read on demand.
    already_answered: dict[str, bool] = {}
    seen_assistant = False
    for message in reversed(messages):
        attachment_id = (message.metadata or {}).get("attachment_id")
        if attachment_id:
            already_answered[attachment_id] = seen_assistant
        if message.role == "assistant":
            seen_assistant = True

    if not already_answered:
        return

    valid_ids: list[UUID] = []
    for raw in already_answered:
        try:
            valid_ids.append(UUID(raw))
        except (ValueError, TypeError):
            logger.debug("[History] Ignoring malformed attachment id %r", raw)

    known = {
        str(a.id): a
        for a in ChatAttachment.objects.filter(id__in=valid_ids)
    } if valid_ids else {}

    for message in messages:
        attachment_id = (message.metadata or {}).get("attachment_id")
        if not attachment_id:
            continue

        attachment = known.get(attachment_id)
        if attachment is None:
            message.content += (
                f"\n\n[RESOURCE DELETED: the file referenced here "
                f"(ID: {attachment_id}) is no longer available.]"
            )
            continue

        text = attachment.extracted_text
        if not text:
            continue

        if supports_docs:
            message.content += f"\n\n[RESOURCE: {attachment.filename} (sent to the model directly)]"
        elif not already_answered[attachment_id]:
            message.content += (
                f"\n\n[FULL RESOURCE CONTENT: {attachment.filename}]\n{text}\n"
                f"[END FULL CONTENT]"
            )
        else:
            message.content += (
                f"\n\n[RESOURCE: FILE] - ID: {attachment_id}\n"
                f"Preview: {text[:LARGE_FILE_PREVIEW_LENGTH]}...\n"
                f"[You have already read \"{attachment.filename}\" in full. To cite it "
                f"precisely, call read_attachment_text with ID {attachment_id}.]"
            )


def _enrich_with_sources(messages: Sequence[ChatMessage]) -> None:
    """Re-attach the citations from earlier research turns, in place.

    Only the most recent few keep their snippets — older ones become bare links.
    Replaying every snippet from every past search fills the window with results
    the model has already used.
    """
    research_turns = [
        index for index, message in enumerate(messages)
        if message.role == "assistant" and (message.metadata or {}).get("sources")
    ]
    detailed = set(research_turns[-3:])

    for index in research_turns:
        message = messages[index]
        lines = []
        for source in (message.metadata or {}).get("sources", [])[:8]:
            title, url = source.get("title", "Source"), source.get("url", "#")
            if index in detailed:
                lines.append(f"- [{title}]({url}) — {source.get('snippet', '')[:500]}")
            else:
                lines.append(f"- [{title}]({url}) (older turn, reference only)")

        message.content += (
            "\n\n<context_metadata type=\"historical_research\">\n"
            "[SOURCES ALREADY REVIEWED]\n" + "\n".join(lines) +
            "\n[Call read_url if you need the full text again. Do not repeat this "
            "list to the user.]\n</context_metadata>"
        )


@sync_to_async
def load_history(
    session: ChatSession, *, exclude_id: int | None, supports_docs: bool = False
) -> list[ChatMessage]:
    """
    Load the recent conversation, enriched with attachment and source context.

    The window counts *conversational* turns only. System rows are the upload
    markers carrying `metadata['attachment_id']`, so counting them against the
    limit would let a handful of uploads push out the actual conversation, while
    excluding them outright would silently drop every attachment from context.
    Take the turns, then re-admit the system rows falling within their span.
    """
    turns = list(
        ChatMessage.objects
        .filter(session=session, role__in=["user", "assistant"])
        .exclude(id=exclude_id)
        .order_by("-created_at")[:HISTORY_WINDOW]
    )
    system_rows = list(
        ChatMessage.objects
        .filter(session=session, role="system", created_at__gte=turns[-1].created_at)
        .exclude(id=exclude_id)
    ) if turns else []

    messages = sorted(turns + system_rows, key=lambda m: m.created_at)
    _enrich_with_attachments(messages, supports_docs=supports_docs)
    _enrich_with_sources(messages)
    return messages


def to_wire_history(
    messages: Iterable[ChatMessage], *, max_tokens: int = MAX_CONTEXT_TOKENS
) -> list[dict[str, str]]:
    """
    Render history as OpenAI-shaped messages within a token budget.

    Walks newest-first so that when the budget runs out it is the oldest turns
    that are lost. Assistant turns with a stored summary contribute the summary
    plus the id needed to fetch the rest.
    """
    wire: list[dict[str, str]] = []
    used = 0

    for message in reversed(list(messages)):
        if message.role not in ("user", "assistant"):
            continue

        summary = (message.metadata or {}).get("summary")
        if message.role == "assistant" and summary:
            content = (
                f"[SUMMARY of message {message.id}]: {summary}\n\n"
                f"(Call get_chat_message_full_text(message_id={message.id}) for the full text.)"
            )
        else:
            content = message.content

        cost = estimate_tokens(content)
        if used + cost > max_tokens:
            break
        wire.append({"role": message.role, "content": content})
        used += cost

    wire.reverse()
    return wire


@sync_to_async
def recent_attachments(messages: Sequence[ChatMessage]) -> list[ChatAttachment]:
    """Attachments referenced by the last few turns, excluding oversized files."""
    ids: set[UUID] = set()
    for message in list(messages)[-_ATTACHMENT_LOOKBACK:]:
        raw = (message.metadata or {}).get("attachment_id")
        if not raw:
            continue
        try:
            ids.add(UUID(raw))
        except (ValueError, TypeError):
            continue

    if not ids:
        return []
    return list(ChatAttachment.objects.filter(id__in=ids).exclude(is_large_file=True))


# ── Model capability gating ──────────────────────────────────────────────────

async def partition_attachments(
    attachments: Sequence[ChatAttachment], *, model: str
) -> tuple[list[ChatAttachment], list[dict[str, Any]]]:
    """
    Split attachments into what this model can ingest and what it cannot.

    Sending an image to a text-only model does not degrade gracefully: it is
    either a 400 or — worse — the image is dropped and the model answers
    confidently about a picture it never received. Both look to the user like
    the assistant ignored their upload, so blocked entries carry a reason meant
    to be shown to them.
    """
    if not attachments:
        return [], []

    from nodes.models import AIModel

    entry = await AIModel.objects.filter(value=model, is_active=True).afirst()

    sendable: list[ChatAttachment] = []
    blocked: list[dict[str, Any]] = []

    for attachment in attachments:
        kind = (attachment.file_type or "other").lower()

        if kind in _TEXT_EXTRACTED_TYPES:
            sendable.append(attachment)
            continue

        capability = _REQUIRED_CAPABILITY.get(kind)
        if capability is None:
            # No ingestion path exists at all, so this is not a per-model
            # limitation and switching models will not help.
            blocked.append({
                "filename": attachment.filename,
                "file_type": kind,
                "reason": f"'{kind}' attachments cannot be read by any model here.",
                "switch_model_helps": False,
            })
        elif entry is not None and getattr(entry, capability, False):
            sendable.append(attachment)
        else:
            blocked.append({
                "filename": attachment.filename,
                "file_type": kind,
                "reason": f"The selected model ({model}) cannot read {kind} input.",
                "switch_model_helps": True,
            })

    return sendable, blocked


def describe_blocked(blocked: Sequence[dict[str, Any]]) -> str:
    """Render the blocked list as a line the user can act on."""
    if not blocked:
        return ""

    switchable = [b for b in blocked if b.get("switch_model_helps")]
    unsupported = [b for b in blocked if not b.get("switch_model_helps")]
    parts: list[str] = []

    if switchable:
        names = ", ".join(f"**{b['filename']}**" for b in switchable)
        kinds = ", ".join(sorted({b["file_type"] for b in switchable}))
        parts.append(
            f"{names} could not be read because the selected model does not accept "
            f"{kinds} input. Switch to a multimodal model to use "
            f"{'them' if len(switchable) > 1 else 'it'}."
        )
    if unsupported:
        names = ", ".join(f"**{b['filename']}**" for b in unsupported)
        parts.append(f"{names} cannot be read by any available model.")

    return " ".join(parts)
