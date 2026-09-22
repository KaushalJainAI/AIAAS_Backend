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
    #: Files the turn wrote or edited in the user's tree. Carries the whole
    #: list every time, for the reason TODOS_UPDATE does: a second write to the
    #: same file updates its entry rather than adding one.
    FILES_UPDATE = "files_update"
    ASK_PERMISSION = "ask_permission"
    ERROR = "error"
    DONE = "done"
    #: A delegated agent run started by `/agent`: status, todos, files and a
    #: link to `/runs/:id` while going; the answer when it lands. The run card
    #: renders from these frames live and from `metadata.agent_run` on reload.
    AGENT_RUN = "agent_run"
    #: A command card: mission, status, cost, memory, schedule preview, goal
    #: confirm, findings, help. Rendered live from the frame and on reload
    #: from `metadata.command_card`.
    COMMAND_CARD = "command_card"
    #: Steers that arrived after the run's last tool boundary, so the model never
    #: read them. Sent after DONE, carrying `messages` as the user wrote them, so
    #: the client can put them back in front of the user. Before this, they sat in
    #: the mailbox and landed mid-way through the session's *next* turn.
    STEERS_RETURNED = "steers_returned"
    #: One coding-task worker's state, for the plan panel. Published to the
    #: lead's sink (chat SSE) and onto the parent execution's channel (runs):
    #: `{task_id, handle, label, agent, status, title, spend}`. Throttled to at
    #: most 1 per second per task — elapsed timers live in a leaf component so
    #: these frames never re-render the transcript.
    TASK_UPDATE = "task_update"
    #: Files currently leased on a project, for the panel's lock list:
    #: `{project, leases: [{pattern, holder_label, task_id}]}`.
    LEASE_UPDATE = "lease_update"
    #: One file a worker wrote, flashing in the panel's change list:
    #: `{path, change_id, by_label}`. The same event the C3 bus carries — one
    #: copy, consumed twice.
    CODE_CHANGE = "code_change"


class EventSink(Protocol):
    """Receives events from the agent. Implementations must not raise."""

    async def __call__(self, event: Event, payload: dict[str, Any]) -> None: ...


async def null_sink(event: Event, payload: dict[str, Any]) -> None:
    """Sink for non-streaming callers. Accepts and discards."""
    return None
