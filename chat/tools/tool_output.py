"""
One ceiling on how much tool output reaches the model, and a way back to the rest.

Every tool that knows what its own result costs already caps it. Nothing capped
the ones that don't — an MCP tool answers from a third-party server, and its
response arrived in the transcript at whatever size that server chose. Past the
context limit `llm.clamp_input` would trim it from the middle without telling
the model, so a truncated page and a short page looked identical.

The contract here is the one thing that fixes: the model gets a bounded preview
that *says* it is bounded, plus the id of the stored full text and a tool that
reads any window of it. Bounding is enforced centrally, after the tool has had
its own chance to shape the result, so a tool may still return something smarter
than head-and-tail; it just cannot return something unbounded.

Storage is best-effort. A tool call that succeeded stays succeeded even if the
spill cannot be written — the model is then told plainly that the remainder is
gone, rather than being handed a preview pointing at an id that does not exist.
"""
from __future__ import annotations

import json
import logging
from datetime import timedelta
from typing import Any

from django.utils import timezone

from workflow_backend.thresholds import (
    RECALL_MAX_MATCHES,
    RECALL_MAX_TOTAL_CHARS,
    RECALL_SCAN_LIMIT,
    RECALL_SNIPPET_CHARS,
    TOOL_OUTPUT_CHAR_LIMIT,
    TOOL_OUTPUT_PREVIEW_CHARS,
    TOOL_OUTPUT_RETENTION_HOURS,
)

logger = logging.getLogger(__name__)

#: `tool_name` on rows written by the curator rather than by a tool. The store
#: is shared deliberately: "what did this run drop" and "what did a tool return
#: too much of" are the same question to a model that has lost the text, and one
#: store means one retention policy, one ownership check and one reader.
ARCHIVE_TOOL_NAME = "context:archive"

#: How much of one stored output `read_tool_output` hands back per call. Smaller
#: than the spill threshold on purpose: paging exists so the model can choose
#: which part it needs, not so it can reassemble the whole thing in context.
READ_WINDOW_CHARS = 12_000


def _omission_notice(omitted: int, output_id: str | None) -> str:
    if output_id is None:
        return (
            f"\n\n[... {omitted:,} characters omitted. The full text could not be "
            f"stored, so the omitted part is unrecoverable — do not assume the "
            f"visible part is complete. Narrow the request and call the tool "
            f"again if you need what is missing. ...]\n\n"
        )
    return (
        f"\n\n[... {omitted:,} characters omitted. The complete output is stored "
        f"as tool_output id '{output_id}'. Call read_tool_output with that id and "
        f"an offset to read any part of it. Do not treat the visible text as the "
        f"whole result. ...]\n\n"
    )


def _head_tail(text: str, output_id: str | None) -> str:
    """Keep both ends: the start says what this is, the end says how it finished."""
    keep = TOOL_OUTPUT_PREVIEW_CHARS // 2
    omitted = len(text) - (keep * 2)
    return text[:keep] + _omission_notice(omitted, output_id) + text[-keep:]


def _shrink_text_field(payload: dict, output_id: str | None) -> str | None:
    """
    Bound a `{"type": ..., "text": ...}` result by shrinking `text` alone.

    Most tools here return that shape, and it is nearly always the `text` field
    that is oversized while the siblings — sources, image lists, a result type —
    are small and are what the model uses to cite or follow up. Trimming the
    whole JSON blob head-and-tail would corrupt it into unparseable prose and
    throw those away with it.
    """
    text = payload.get("text")
    if not isinstance(text, str):
        return None

    scaffold = len(json.dumps({**payload, "text": ""}))
    budget = TOOL_OUTPUT_CHAR_LIMIT - scaffold
    if budget < TOOL_OUTPUT_PREVIEW_CHARS:
        # The other fields are the bulk; shrinking `text` cannot save this one.
        return None

    keep = TOOL_OUTPUT_PREVIEW_CHARS // 2
    omitted = len(text) - (keep * 2)
    payload = {
        **payload,
        "text": text[:keep] + _omission_notice(omitted, output_id) + text[-keep:],
    }
    return json.dumps(payload)


async def _spill(name: str, text: str, context: dict[str, Any]) -> str | None:
    """Persist the full text; return its id, or None if it could not be kept."""
    from chat.models import ToolOutput

    try:
        row = await ToolOutput.objects.acreate(
            user_id=context.get("user_id"),
            session_key=str(context.get("session_id") or "")[:64],
            turn_id=str(context.get("turn_id") or "")[:64],
            tool_name=name[:160],
            content=text,
            total_chars=len(text),
            expires_at=timezone.now() + timedelta(hours=TOOL_OUTPUT_RETENTION_HOURS),
        )
        return row.id
    except Exception:  # noqa: BLE001
        logger.exception("[ToolOutput] Could not store oversized result from %s", name)
        return None


async def bound(name: str, output: Any, context: dict[str, Any]) -> str:
    """
    Return what the model should see for this tool result.

    Called after the tool ran and after its UI side effects have been applied,
    so shaping here cannot break anything downstream — by this point the only
    remaining consumer of the string is the model.
    """
    text = output if isinstance(output, str) else str(output)
    if len(text) <= TOOL_OUTPUT_CHAR_LIMIT:
        return text

    logger.info(
        "[ToolOutput] %s returned %d chars, over the %d limit",
        name, len(text), TOOL_OUTPUT_CHAR_LIMIT,
    )
    output_id = await _spill(name, text, context)

    try:
        payload = json.loads(text)
    except (json.JSONDecodeError, TypeError, ValueError):
        payload = None

    if isinstance(payload, dict):
        shrunk = _shrink_text_field(payload, output_id)
        if shrunk is not None:
            return shrunk

    return _head_tail(text, output_id)


async def read(output_id: str, offset: int, context: dict[str, Any]) -> str:
    """
    Hand back one window of a stored result.

    Scoped to the owner *and* the session that produced it. The ids are short
    enough to guess at, so ownership is a filter on the query rather than a
    check after the fetch — a miss and a mismatch return the same thing.
    """
    from chat.models import ToolOutput

    row = await ToolOutput.objects.filter(
        id=output_id,
        user_id=context.get("user_id"),
        session_key__in=readable_scopes(context),
    ).afirst()

    if row is None:
        return (
            f"Error: no stored output with id '{output_id}' in this conversation. "
            f"Ids are only valid for the conversation that produced them."
        )

    if row.expires_at <= timezone.now():
        return (
            f"Error: stored output '{output_id}' has expired and is no longer "
            f"available. Re-run the tool if you still need it."
        )

    offset = max(0, offset)
    if offset >= row.total_chars:
        return (
            f"Offset {offset} is past the end of output '{output_id}' "
            f"({row.total_chars:,} characters). Nothing to read."
        )

    window = row.content[offset:offset + READ_WINDOW_CHARS]
    end = offset + len(window)
    remaining = row.total_chars - end
    footer = (
        f"\n\n[Showing characters {offset:,}-{end:,} of {row.total_chars:,}. "
        + (
            f"{remaining:,} remain — call read_tool_output again with offset={end}.]"
            if remaining else "End of output.]"
        )
    )
    return window + footer


# ── Archived context ─────────────────────────────────────────────────────────

def readable_scopes(context: dict[str, Any]) -> list[str]:
    """Every archive this caller may read: its own, plus any it was handed.

    A delegated worker gets its parent's scope in `archive_scopes`, one hop and
    read-only. Without it the two halves of this design work against each other:
    the parent curates a detail away into an archive keyed by *its* thread, then
    delegates a task that needs it, and the worker — a fresh thread — cannot
    reach the very text the parent could no longer restate.

    Writing still goes to the caller's own scope alone (`archive`, `_spill`),
    so a worker can never put anything into its parent's archive. Ownership is
    still checked separately on every query: a scope is a session key, not a
    permission.
    """
    own = str(context.get("session_id") or "")[:64]
    scopes = [own] if own else []
    for extra in context.get("archive_scopes") or ():
        key = str(extra or "")[:64]
        if key and key not in scopes:
            scopes.append(key)
    return scopes



async def archive(
    label: str, text: str, context: dict[str, Any]
) -> str | None:
    """
    Store text the curator is about to remove from a transcript.

    Same table, same retention and same ownership rules as a tool spill, because
    the model's question is identical in both cases — "there is something I was
    shown and can no longer see; where is it?" — and a second store would mean a
    second expiry, a second scope check and a second reader to keep in step.

    Best-effort, like `_spill`: failing to archive must not fail the run. The
    caller checks the return value, and says nothing about recall when it is
    None rather than pointing the model at an id that was never written.
    """
    from chat.models import ToolOutput

    try:
        row = await ToolOutput.objects.acreate(
            user_id=context.get("user_id"),
            session_key=str(context.get("session_id") or "")[:64],
            turn_id=str(context.get("turn_id") or "")[:64],
            tool_name=f"{ARCHIVE_TOOL_NAME}:{label}"[:160],
            content=text,
            total_chars=len(text),
            expires_at=timezone.now() + timedelta(hours=TOOL_OUTPUT_RETENTION_HOURS),
        )
        return row.id
    except Exception:  # noqa: BLE001
        logger.exception("[ToolOutput] Could not archive curated context (%s)", label)
        return None


def _score(haystack: str, terms: list[str]) -> tuple[int, int]:
    """(distinct terms present, position of the first hit).

    Distinct terms first so a row mentioning three of the query's words beats
    one repeating a single word thirty times, which is what a raw frequency
    count would prefer.
    """
    lowered = haystack.lower()
    hits = [lowered.find(term) for term in terms]
    found = [position for position in hits if position >= 0]
    if not found:
        return 0, -1
    return len(found), min(found)


def _window(text: str, position: int) -> str:
    """A readable slice around a hit, with the ends marked when they are cut."""
    half = RECALL_SNIPPET_CHARS // 2
    start = max(0, position - half)
    end = min(len(text), start + RECALL_SNIPPET_CHARS)
    snippet = text[start:end]
    if start:
        snippet = f"...{snippet}"
    if end < len(text):
        snippet = f"{snippet}..."
    return snippet


async def recall(query: str, context: dict[str, Any]) -> str:
    """
    Search everything stored for this run — archived context and tool spills
    alike — and return the best few windows, each with the id that pages it.

    Scanned in Python over a capped number of rows for the same reason
    `search_conversation_history` is: the store is small per session, the match
    is a scoring function rather than a predicate, and SQLite has no ranking to
    push it down to.
    """
    from chat.models import ToolOutput

    terms = [t for t in {w.lower() for w in query.split()} if len(t) > 2]
    if not terms:
        return (
            "Error: 'query' needs at least one word of three characters or more. "
            "Search for a term you expect to appear in the dropped text."
        )

    rows = [
        row async for row in ToolOutput.objects.filter(
            user_id=context.get("user_id"),
            session_key__in=readable_scopes(context),
            expires_at__gt=timezone.now(),
        ).order_by("-created_at")[:RECALL_SCAN_LIMIT]
    ]
    if not rows:
        return (
            "Nothing has been archived in this run yet, so there is nothing "
            "recall_context can return. Everything you were shown is still visible."
        )

    scored = []
    for row in rows:
        count, position = _score(row.content, terms)
        if count:
            scored.append((count, -row.total_chars, row, position))
    if not scored:
        return (
            f"No archived text in this run matches {query!r}. "
            f"{len(rows)} archived item(s) were searched."
        )

    scored.sort(key=lambda item: (-item[0], item[1]))

    parts: list[str] = []
    used = 0
    for _, _, row, position in scored[:RECALL_MAX_MATCHES]:
        snippet = _window(row.content, position)
        block = (
            f"[archived: {row.tool_name} · id '{row.id}' · "
            f"{row.total_chars:,} chars total]\n{snippet}"
        )
        if used + len(block) > RECALL_MAX_TOTAL_CHARS:
            break
        parts.append(block)
        used += len(block)

    footer = (
        "\n\n[These are windows, not the whole text. Call read_tool_output with "
        "an id above and an offset to read any part of one in full.]"
    )
    return "\n\n---\n\n".join(parts) + footer
