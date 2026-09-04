"""
What the assistant knows about the user, across sessions.

Distinct from `conversation.py`, which reads back *this* conversation. That is
recall; this is memory. A fact stored here outlives the session, the chat
window and the run, and is shown to the model on every later turn — which is
why the tool description spends most of its words on what *not* to store.
"""
from __future__ import annotations

import json
import logging
from typing import Dict

from asgiref.sync import sync_to_async

from .registry import tool

logger = logging.getLogger(__name__)


def _user(context: Dict):
    from django.contrib.auth import get_user_model

    user_id = context.get("user_id")
    if not user_id:
        return None
    return get_user_model().objects.filter(id=user_id).first()


@tool({
    "type": "function",
    "function": {
        "name": "remember_about_user",
        "description": (
            "Store one durable fact about this user so you still know it in "
            "future conversations. Store something ONLY if it would change how "
            "you answer later: their role or expertise, how they like answers "
            "(short, detailed, code-first), their timezone or language, "
            "long-running projects, tools and stacks they use, constraints "
            "they have told you about. Do NOT store what they asked in this "
            "conversation, anything you can look up, anything they told you "
            "once in passing, or anything sensitive they did not clearly want "
            "kept. Everything stored here is shown to you on every future "
            "turn, so a useless fact costs the user money for ever. One fact "
            "per call, written as a short statement."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "text": {
                    "type": "string",
                    "description": "The fact, as a short statement. E.g. 'Prefers concise answers with code first'.",
                },
                "category": {
                    "type": "string",
                    "enum": ["profile", "preference", "project", "context"],
                    "description": (
                        "profile = who they are, preference = how they like to "
                        "work, project = what they are working on, context = "
                        "anything else worth keeping."
                    ),
                },
            },
            "required": ["text"],
            "additionalProperties": False,
        },
    },
}, effect="reversible")
async def remember_about_user(args: Dict, context: Dict) -> str:
    """Write one fact.

    `reversible` rather than `read`: it changes something outside this run that
    persists, and `forget_about_user` is the undo. Not `sensitive` — a prompt
    on every remembered preference would train users to click through
    approvals, which is what makes the prompts on genuinely risky tools
    worthless.
    """
    from core import memory as user_memory

    user = await sync_to_async(_user)(context)
    if user is None:
        return json.dumps({"error": "No user in context; nothing was stored."})

    text = (args.get("text") or "").strip()
    if not text:
        return json.dumps({"error": "Give the fact to remember."})

    row, created = await sync_to_async(user_memory.remember)(
        user, text, (args.get("category") or "context").strip().lower(),
    )
    if row is None:
        return json.dumps({"error": "Give the fact to remember."})

    return json.dumps({
        "stored": row.text,
        "category": row.category,
        # Said plainly so the model does not "correct" a repeat by rewording it
        # and creating a near-duplicate of a fact it already had.
        "already_known": not created,
    })


@tool({
    "type": "function",
    "function": {
        "name": "forget_about_user",
        "description": (
            "Remove a fact you previously stored about this user. Use it when "
            "they ask you to forget something, or when you learn a stored fact "
            "is now wrong — correcting a fact means forgetting the old one and "
            "remembering the new one, not storing both."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "text": {
                    "type": "string",
                    "description": "The exact fact to remove, as shown to you.",
                },
            },
            "required": ["text"],
            "additionalProperties": False,
        },
    },
}, effect="reversible")
async def forget_about_user(args: Dict, context: Dict) -> str:
    from core import memory as user_memory

    user = await sync_to_async(_user)(context)
    if user is None:
        return json.dumps({"error": "No user in context."})

    removed = await sync_to_async(user_memory.forget)(user, args.get("text") or "")
    if not removed:
        # Named rather than reported as success: a model told "done" would tell
        # the user their fact was forgotten when it is still there and will
        # reappear in the next system prompt.
        return json.dumps({
            "removed": 0,
            "error": "No stored fact matches that text exactly.",
        })
    return json.dumps({"removed": removed})
