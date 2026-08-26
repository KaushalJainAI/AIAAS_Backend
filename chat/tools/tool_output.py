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
    TOOL_OUTPUT_CHAR_LIMIT,
    TOOL_OUTPUT_PREVIEW_CHARS,
    TOOL_OUTPUT_RETENTION_HOURS,
)

logger = logging.getLogger(__name__)

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
        session_key=str(context.get("session_id") or "")[:64],
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
