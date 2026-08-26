"""
Tools that read back from this conversation's own record.

All four answer "what was already said or attached here" — transcript search,
an untruncated message, an attachment's text, a spilled tool result. They are
gated rather than always offered: two need memory to be on, and
`read_tool_output` needs something to have actually spilled.
"""
from __future__ import annotations

import json
import logging
import re

from typing import Dict, NamedTuple

from workflow_backend.thresholds import (
    HISTORY_SEARCH_MAX_MATCHES,
    HISTORY_SEARCH_SNIPPET_CHARS,
    HISTORY_SEARCH_MAX_TOTAL_CHARS,
    HISTORY_SEARCH_MAX_PATTERN_LEN,
    HISTORY_SEARCH_SCAN_LIMIT,
)

from .registry import tool


class _Hit(NamedTuple):
    """One row's best match. Named because it used to be a bare 4-tuple
    compared by `best[0]` and unpacked fifteen lines away."""
    score: float
    kind: str
    text: str
    position: int


def _haystacks(row: dict, scope: str) -> list[tuple[str, str]]:
    """The (kind, text) pairs `scope` says to search on one row."""
    found: list[tuple[str, str]] = []
    if scope in ("all", "messages"):
        found.append(("message", row.get('content') or ""))
    if scope in ("all", "reasoning"):
        meta = row.get('metadata') or {}
        if isinstance(meta, dict) and (meta.get('thinking') or ""):
            found.append(("reasoning", meta['thinking']))
    return found


def _best_hit(haystacks: list[tuple[str, str]], terms: list[str]) -> '_Hit | None':
    """The strongest match among one row's haystacks, or None.

    Ranked by how much of the query a row accounts for; ties go to the row
    that matched on what was actually said over what was merely thought.
    """
    best: _Hit | None = None
    for kind, text in haystacks:
        low = text.lower()
        hits = [t for t in terms if t in low]
        if not hits:
            continue
        score = len(hits) / len(terms) + (0.1 if kind == "message" else 0)
        if best is None or score > best.score:
            best = _Hit(score, kind, text, min(low.find(t) for t in hits))
    return best


def _snippet(text: str, position: int) -> str:
    """A window of `text` centred on `position`, ellipsed where it was cut."""
    half = HISTORY_SEARCH_SNIPPET_CHARS // 2
    start = max(0, position - half)
    end = min(len(text), position + half)
    snippet = text[start:end]
    if start > 0:
        snippet = "..." + snippet
    if end < len(text):
        snippet = snippet + "..."
    return snippet



logger = logging.getLogger(__name__)


async def has_spilled_output(user_id: int | None, session_key: str | None) -> bool:
    """Whether this conversation has any unexpired oversized result stored."""
    if not session_key:
        return False
    try:
        from django.utils import timezone
        from ..models import ToolOutput

        return await ToolOutput.objects.filter(
            user_id=user_id,
            session_key=str(session_key)[:64],
            expires_at__gt=timezone.now(),
        ).aexists()
    except Exception as e:  # noqa: BLE001
        # Withhold rather than guess: an unreachable table means the read
        # would fail too, and the tool would be advertised for nothing.
        logger.warning(f"Could not check stored tool outputs: {e}")
        return False


@tool({
        "type": "function",
        "function": {
            "name": "search_conversation_history",
            "description": "Search the ENTIRE history of this conversation, including turns that are no longer in your visible context and your own earlier reasoning. Only the most recent turns are replayed to you automatically — everything older is still stored and only reachable through this tool. Use it whenever the user refers to something you cannot see ('the number I gave you earlier', 'what we decided yesterday'). Prefer this over telling the user you do not remember.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Words or a phrase to look for. Matching is case-insensitive and term-based: results are ranked by how many of your terms they contain, so give several specific words rather than a sentence."
                    },
                    "scope": {
                        "type": "string",
                        "enum": [
                            "all",
                            "messages",
                            "reasoning"
                        ],
                        "description": "'messages' searches what was said, 'reasoning' searches your own stored thinking from earlier turns, 'all' searches both. Defaults to 'all'."
                    },
                    "role": {
                        "type": "string",
                        "enum": [
                            "any",
                            "user",
                            "assistant"
                        ],
                        "description": "Restrict to one speaker. Defaults to 'any'."
                    }
                },
                "required": [
                    "query"
                ],
                "additionalProperties": False
            }
        }
    },
    requires="memory",
)
async def search_conversation_history(args: Dict, context: Dict) -> str:
    """
    Grep-style lookup over everything this conversation has ever said or thought.

    Only the last HISTORY_WINDOW turns are replayed into the prompt; this is
    how the model reaches the rest without us paying for it every turn.

    Two design decisions worth stating, because both look like shortcuts:

    1. Term matching, not regex. The pattern comes from the model, and Python's
       `re` has no evaluation timeout, so a backtracking pattern like (a+)+$ run
       over stored message text pins a worker with no way to interrupt it. The
       retrieval quality difference is small; the availability difference is not.

    2. Scanning in Python rather than filtering in the DB. Half of what we
       search is metadata['thinking'], and JSON-key containment lookups differ
       between Postgres and the SQLite used by the test suite. A bounded scan
       behaves identically on both. HISTORY_SEARCH_SCAN_LIMIT keeps it cheap.
    """
    from asgiref.sync import sync_to_async
    from ..models import ChatMessage

    query = (args.get("query") or "").strip()
    if not query:
        return json.dumps({"error": "Missing query."})
    if len(query) > HISTORY_SEARCH_MAX_PATTERN_LEN:
        query = query[:HISTORY_SEARCH_MAX_PATTERN_LEN]

    scope = (args.get("scope") or "all").lower()
    if scope not in ("all", "messages", "reasoning"):
        scope = "all"
    role = (args.get("role") or "any").lower()
    if role not in ("any", "user", "assistant"):
        role = "any"

    session_id = context.get("session_id")
    user_id = context.get("user_id")
    if not session_id:
        return json.dumps({"error": "No conversation context available for search."})

    terms = [t for t in re.split(r'\s+', query.lower()) if len(t) > 1]
    if not terms:
        return json.dumps({"error": "Query too short to search."})

    def _search() -> list[dict]:
        qs = ChatMessage.objects.filter(session_id=session_id)
        # Ownership: the session must belong to the caller. Scoping by
        # session_id alone would let a leaked/guessed UUID read another
        # user's conversation.
        if user_id:
            qs = qs.filter(session__user_id=user_id)
        if role != "any":
            qs = qs.filter(role=role)
        else:
            qs = qs.filter(role__in=["user", "assistant"])

        rows = list(
            qs.order_by('-created_at')
            .values('id', 'role', 'content', 'metadata', 'created_at')[:HISTORY_SEARCH_SCAN_LIMIT]
        )

        scored = []
        for r in rows:
            hit = _best_hit(_haystacks(r, scope), terms)
            if hit is None:
                continue
            scored.append({
                "score": round(hit.score, 3),
                "message_id": r['id'],
                "role": r['role'],
                "found_in": hit.kind,
                "timestamp": r['created_at'].isoformat(),
                "snippet": _snippet(hit.text, hit.position),
            })

        scored.sort(key=lambda x: (-x["score"], -x["message_id"]))
        return scored[:HISTORY_SEARCH_MAX_MATCHES]

    try:
        matches = await sync_to_async(_search)()
    except Exception as e:
        logger.error(f"search_conversation_history failed: {e}")
        return json.dumps({"error": f"History search failed: {e}"})

    if not matches:
        return json.dumps({
            "matches": [],
            "message": (
                "No earlier messages matched those terms. Try fewer or different "
                "keywords before concluding the information was never provided."
            ),
        })

    # Final ceiling. The whole point of this tool is to keep the context small,
    # so it must not be able to return more than the window it is protecting —
    # drop whole matches rather than truncating mid-snippet.
    payload, total = [], 0
    for m in matches:
        cost = len(m["snippet"])
        if total + cost > HISTORY_SEARCH_MAX_TOTAL_CHARS:
            break
        payload.append(m)
        total += cost

    return json.dumps({
        "matches": payload,
        "returned": len(payload),
        "truncated": len(payload) < len(matches),
        "hint": "Call get_chat_message_full_text(message_id=...) for the full text of any match.",
    })


@tool({
        "type": "function",
        "function": {
            "name": "get_chat_message_full_text",
            "description": "Fetch the full original content of a previous assistant message that was summarized. Use this if the summary in the history is missing details you need.",
            "parameters": {
                "type": "object",
                "properties": {
                    "message_id": {
                        "type": "integer",
                        "description": "The ID of the message to read."
                    }
                },
                "required": [
                    "message_id"
                ],
                "additionalProperties": False
            }
        }
    },
    requires="memory",
)
async def get_chat_message_full_text(args: Dict, context: Dict) -> str:
    from ..models import ChatMessage
    msg_id = args.get("message_id")
    if not msg_id:
        return "Error: Missing message_id"
    try:
        user_id = context.get("user_id")
        msg = await ChatMessage.objects.select_related('session').filter(
            id=int(msg_id)
        ).afirst()
        if not msg:
            return f"Error: Message with ID {msg_id} not found."
        if user_id and msg.session and msg.session.user_id != user_id:
            return "Error: Access denied — message does not belong to your session."
        return json.dumps({"message_id": msg_id, "content": msg.content})
    except Exception as e:
        return f"Error: Failed to read message from database: {str(e)}"


@tool({
        "type": "function",
        "function": {
            "name": "read_attachment_text",
            "description": "Fetch the full extracted text of a previously uploaded file/attachment from the database. Use this if the preview snippet in the context is insufficient.",
            "parameters": {
                "type": "object",
                "properties": {
                    "attachment_id": {
                        "type": "string",
                        "description": "The UUID of the attachment to read."
                    }
                },
                "required": [
                    "attachment_id"
                ],
                "additionalProperties": False
            }
        }
    })
async def read_attachment_text(args: Dict, context: Dict) -> str:
    from uuid import UUID
    from ..models import ChatAttachment
    att_id = args.get("attachment_id")
    if not att_id:
        return "Error: Missing attachment_id"
    try:
        user_id = context.get("user_id")
        att = await ChatAttachment.objects.select_related('session').filter(
            id=UUID(att_id)
        ).afirst()
        if not att:
            return f"Error: Attachment with ID {att_id} not found."
        # Ownership comes off `session`, not `message`. An upload creates the
        # attachment before any message references it and `message` is
        # SET_NULL, so it is null for most rows — and a guard that starts
        # `if att.message and ...` passes every one of them. `session` is
        # non-null for the life of the row. Reachable directly through
        # /api/chat/execute-tool/, so this is the only check there is.
        if user_id and att.session.user_id != user_id:
            return "Error: Access denied — attachment does not belong to your session."
        return json.dumps({
            "attachment_id": att_id,
            "filename": att.filename,
            "content": att.extracted_text
        })
    except Exception as e:
        return f"Error: Failed to read attachment from database: {str(e)}"


@tool({
        "type": "function",
        "function": {
            "name": "read_tool_output",
            "description": "Read part of a tool result that was too large to show you in full. When a result is bounded you are told its tool_output id and how much was omitted; pass that id here, with an offset, to read any window of the complete text. Use it when the omitted part could change your answer — do not guess at what was cut, and do not re-run the original tool hoping for a shorter result.",
            "parameters": {
                "type": "object",
                "properties": {
                    "output_id": {
                        "type": "string",
                        "description": "The tool_output id named in the omission notice."
                    },
                    "offset": {
                        "type": "integer",
                        "description": "Character position to read from (default 0). The footer of each window tells you the next offset."
                    }
                },
                "required": [
                    "output_id"
                ],
                "additionalProperties": False
            }
        }
    },
    requires="spill",
)
async def read_tool_output(args: Dict, context: Dict) -> str:
    from .tool_output import read

    output_id = (args.get("output_id") or "").strip()
    if not output_id:
        return "Error: 'output_id' is required."
    try:
        offset = int(args.get("offset") or 0)
    except (TypeError, ValueError):
        offset = 0
    return await read(output_id, offset, context)
