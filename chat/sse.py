"""
Server-sent-events transport for the chat agent.

The agent emits typed events into an `EventSink` and knows nothing about HTTP.
This adapts that to `text/event-stream`: `SSEBridge.sink` is handed to the agent,
and `SSEBridge.stream()` yields frames while the work runs.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, AsyncIterator, Coroutine

from .events import Event

logger = logging.getLogger(__name__)

#: How long to wait on the queue before re-checking whether the work finished.
#: Bounds shutdown latency only — events are delivered the moment they arrive.
_POLL_INTERVAL = 0.25

GENERIC_ERROR = "Something went wrong on that turn. Please try again."


def frame(event: Event | str, payload: dict[str, Any] | None = None) -> str:
    """Render one SSE frame. `type` is merged in, matching the client contract."""
    return f"data: {json.dumps({'type': str(event), **(payload or {})}, default=str)}\n\n"


def error_frame(message: str) -> str:
    return frame(Event.ERROR, {"message": message})


class SSEBridge:
    """Queue between an agent run and the HTTP response generator."""

    __slots__ = ("_queue",)

    def __init__(self) -> None:
        self._queue: asyncio.Queue[tuple[Event, dict[str, Any]]] = asyncio.Queue()

    async def sink(self, event: Event, payload: dict[str, Any]) -> None:
        """`EventSink` implementation handed to the agent."""
        await self._queue.put((event, payload))

    async def emit(self, event: Event, **payload: Any) -> None:
        """Emit an event from the caller, ordered with the agent's own."""
        await self.sink(event, payload)

    async def stream(
        self, work: Coroutine[Any, Any, Any], *, on_error: str = GENERIC_ERROR
    ) -> AsyncIterator[str]:
        """
        Run `work` to completion, yielding each event it emits as an SSE frame.

        A failure inside `work` becomes a terminal `error` frame rather than an
        exception through the response body, which the browser would surface as
        a bare network error with nothing to show the user. Client disconnect
        cancels the work instead of leaving it running.
        """
        task = asyncio.ensure_future(work)
        try:
            while not task.done():
                try:
                    event, payload = await asyncio.wait_for(
                        self._queue.get(), timeout=_POLL_INTERVAL
                    )
                except asyncio.TimeoutError:
                    continue
                yield frame(event, payload)

            while not self._queue.empty():  # events emitted just before finishing
                event, payload = self._queue.get_nowait()
                yield frame(event, payload)
        finally:
            if not task.done():
                task.cancel()  # client went away, or the generator was closed

        if task.cancelled():
            return
        if (failure := task.exception()) is not None:
            logger.exception("[SSE] Turn failed", exc_info=failure)
            yield error_frame(on_error)
