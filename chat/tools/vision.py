"""
The `ask_vision` tool: the main agent's question to the vision witness.

The witness itself is `chat.vision`; this is only the tool surface over it.
Offered solely when a witness resolves — see `requires="vision"`.
"""
from __future__ import annotations

import json
import logging

from typing import Dict

from .registry import tool

logger = logging.getLogger(__name__)


@tool({
        "type": "function",
        "function": {
            "name": "ask_vision",
            "description": "Ask a vision-capable assistant a question about an image or visual document you cannot see yourself. It has the file open and remembers your earlier questions about it, so ask specific questions and ask follow-ups when an answer is vague or you need one more detail. Its reply is testimony from another model, not something you saw.",
            "parameters": {
                "type": "object",
                "properties": {
                    "attachment_id": {
                        "type": "string",
                        "description": "The UUID of the attachment to examine."
                    },
                    "question": {
                        "type": "string",
                        "description": "One specific question about the file, e.g. 'what are the four bar values and their axis units?'"
                    }
                },
                "required": [
                    "attachment_id",
                    "question"
                ],
                "additionalProperties": False
            }
        }
    },
    requires="vision",
)
async def ask_vision(args: Dict, context: Dict) -> str:
    """Put a question about an attachment to a model that can see it.

    The ownership check is the same one `_read_attachment_text` makes and for
    the same reason: this is reachable directly through
    /api/chat/execute-tool/, so a crafted attachment_id is otherwise a read
    of another user's uploads — worse here, because the answer is prose
    describing what is in the file.
    """
    from uuid import UUID
    from .. import vision
    from ..models import ChatAttachment

    att_id = (args.get("attachment_id") or "").strip()
    question = (args.get("question") or "").strip()
    if not att_id:
        return "Error: Missing attachment_id"
    if not question:
        return "Error: Missing question — ask something specific about the file."

    user_id = context.get("user_id")
    try:
        att = await ChatAttachment.objects.select_related("session").filter(
            id=UUID(att_id)
        ).afirst()
    except (ValueError, TypeError):
        return f"Error: '{att_id}' is not a valid attachment id."
    except Exception as e:  # noqa: BLE001
        return f"Error: Failed to load attachment: {str(e)}"

    if not att:
        return f"Error: Attachment with ID {att_id} not found."
    if user_id and att.session.user_id != user_id:
        return "Error: Access denied — attachment does not belong to your session."

    answer = await vision.ask(
        att,
        question,
        session_id=str(att.session_id),
        user_id=user_id,
        turn_id=context.get("turn_id") or "",
    )
    return json.dumps({
        "type": "vision_answer",
        "attachment_id": att_id,
        "filename": att.filename,
        "question": question,
        "answer": answer,
    })
