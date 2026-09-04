"""
Server-sent-events rendering for the chat agent.

The agent emits typed events into an `EventSink` and knows nothing about HTTP.
This turns those events into `text/event-stream` frames.

Rendering is all that lives here. Owning the work — starting it, buffering its
events, deciding when it ends — belongs to `runs`, because a turn outlives the
response that happens to be watching it. An earlier `SSEBridge` in this module
tied the two together and cancelled the turn whenever its response generator
closed, which is precisely the behaviour `runs` exists to undo.
"""
from __future__ import annotations

import json
from typing import Any

from chat.turn.events import Event

GENERIC_ERROR = "Something went wrong on that turn. Please try again."


def frame(event: Event | str, payload: dict[str, Any] | None = None) -> str:
    """Render one SSE frame. `type` is merged in, matching the client contract."""
    return f"data: {json.dumps({'type': str(event), **(payload or {})}, default=str)}\n\n"


def error_frame(message: str) -> str:
    return frame(Event.ERROR, {"message": message})
