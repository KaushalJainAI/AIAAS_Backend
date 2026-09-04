"""
The event contract between the chat agent and any transport that streams it.

The agent does not know about SSE. It emits typed events into an `EventSink`;
`views.send_message_stream` adapts those to `text/event-stream` frames. That
separation is what lets the same agent back the non-streaming endpoint, a test,
or a future WebSocket transport without a second copy of the loop.

Event names are the wire names the frontend already switches on — changing a
value here changes the client contract.
"""
from __future__ import annotations

from enum import StrEnum
from typing import Any, Protocol


class Event(StrEnum):
    """Event types emitted during a turn."""

    STATUS = "status"
    THINKING_CHUNK = "thinking_chunk"
    CONTENT_CHUNK = "content_chunk"
    #: Text already streamed turned out to be a preamble to a tool call, not the
    #: answer. The client must clear its live buffer. Emitted only in that case,
    #: so a client that ignores it degrades to showing a stale preamble rather
    #: than breaking.
    CONTENT_RESET = "content_reset"
    AGENT_TRACE = "agent_trace"
    SOURCES_UPDATE = "sources_update"
    IMAGES_UPDATE = "images_update"
    VIDEOS_UPDATE = "videos_update"
    HTML_ARTIFACT = "html_artifact"
    #: A chart the frontend draws from data. Separate from HTML_ARTIFACT
    #: because the payloads are different in kind — markup there, a validated
    #: spec here — and a client that can render one may not render the other.
    CHART = "chart"
    ATTACHMENTS_BLOCKED = "attachments_blocked"
    #: The run's plan changed. Carries the whole list every time, because the
    #: tool that produces it replaces the whole list every time — a client
    #: applying deltas would have to reconstruct state the server never sends.
    TODOS_UPDATE = "todos_update"
    ASK_PERMISSION = "ask_permission"
    ERROR = "error"
    DONE = "done"


class EventSink(Protocol):
    """Receives events from the agent. Implementations must not raise."""

    async def __call__(self, event: Event, payload: dict[str, Any]) -> None: ...


async def null_sink(event: Event, payload: dict[str, Any]) -> None:
    """Sink for non-streaming callers. Accepts and discards."""
    return None
