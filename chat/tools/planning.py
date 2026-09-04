"""
The run's own plan: one tool that replaces it wholesale.

Kept apart from `conversation.py` because it is not a read of anything. It is
the only tool whose entire output is state for the *next* turn of the same run
— see `chat/turn/todos.py` for why that state lives beside the transcript
rather than inside it.
"""
from __future__ import annotations

import json
import logging
from typing import Dict

from chat.turn import todos as todo_state

from .registry import tool

logger = logging.getLogger(__name__)


@tool({
    "type": "function",
    "function": {
        "name": "update_todos",
        "description": (
            "Record or update your plan for this task. Send the COMPLETE list "
            "every time — it replaces the previous one, so anything you leave "
            "out is forgotten. Use it when a task needs several steps: write "
            "the plan before you start, mark an item 'doing' as you begin it "
            "and 'done' the moment it is finished, and mark it 'blocked' with "
            "the reason if you cannot complete it. Do not use it for a task "
            "you can finish in one or two steps. Your open items are shown "
            "back to you every turn, so this is how you remember what you were "
            "doing on a long job."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "todos": {
                    "type": "array",
                    "description": "The complete plan, in the order you intend to work.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "text": {
                                "type": "string",
                                "description": "What this step is. A short label, not a paragraph.",
                            },
                            "status": {
                                "type": "string",
                                "enum": list(todo_state.STATUSES),
                                "description": (
                                    "open = not started, doing = in progress, "
                                    "done = finished, blocked = cannot be done "
                                    "(say why in the text)."
                                ),
                            },
                        },
                        "required": ["text", "status"],
                        "additionalProperties": False,
                    },
                },
            },
            "required": ["todos"],
            "additionalProperties": False,
        },
    },
}, effect="read")
async def update_todos(args: Dict, context: Dict) -> str:
    """Validate and echo the plan back.

    The echo matters: the model has to see what was actually stored, because
    `normalize` drops items over the cap and rewrites a status it does not
    recognise. Returning a bare "ok" would let it believe it recorded twenty-five
    steps when twenty were kept.

    Nothing is written here. `chat/turn/agent.py::_on_todos` takes this result
    and puts it in graph state, which is the same path every other structured
    tool result travels — a tool that reached into the graph itself would be a
    second way for state to change, and the first thing to disagree with the
    checkpoint.
    """
    items = todo_state.normalize(args.get("todos"))
    if not items:
        return json.dumps({
            "error": "Send at least one step, each with text and a status.",
        })

    sent = len(args.get("todos") or [])
    payload = {
        "type": "todos",
        "todos": items,
        "open": len(todo_state.unfinished(items)),
    }
    if sent > len(items):
        payload["note"] = (
            f"{sent - len(items)} item(s) were dropped: the limit is "
            f"{todo_state.MAX_TODOS} steps. Track the work in fewer, larger steps."
        )
    return json.dumps(payload)
