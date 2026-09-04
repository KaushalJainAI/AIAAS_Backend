"""
Chat turns that outlive the HTTP request that started them.

A streamed turn used to be owned by its response: `SSEBridge` cancelled the
work the moment the response generator closed, so a browser reload mid-answer
killed the turn. The user's message had already been committed and the reply
never was, leaving a question with no answer and no way to resume.

A run is therefore held here, keyed by chat session id, owning its own task and
a buffer of every frame it has emitted. An HTTP response is only a *view* of a
run: attaching replays the buffer and then follows live, detaching leaves the
task alone. Only `stop()` — an explicit user action — cancels the work.

This is deliberately the same shape as the client-side registry in
`better-n8n-frontend/src/lib/chatRuns.ts`. The two together are what make a
turn survive both a route change (client keeps reading) and a reload (server
keeps working, client re-attaches).

Scope: the registry is per-process, like the agent's `MemorySaver` checkpointer
in `agent.py`. Production runs a single ASGI process, so that is the whole
application. Under multiple workers a re-attach would have to land on the
worker holding the run; moving the buffer to Redis is the upgrade path.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Awaitable, Callable

from workflow_backend.background import spawn

from .events import Event, EventSink

logger = logging.getLogger(__name__)

#: How long a finished run stays attachable for a client that comes back. It
#: only has to cover the gap between a transcript load and the attach that
#: follows it — past that the answer is in the database and is read from there.
RETAIN_FINISHED_SECONDS = 300

RunStatus = str  # "running" | "done" | "error" | "stopped"

#: Pushed to every listener queue when a run reaches a terminal state.
_SENTINEL = None


@dataclass
class ChatRun:
    """One in-flight (or recently finished) turn for a chat session."""

    key: str
    user_id: int
    status: RunStatus = "running"
    error: str | None = None
    started_at: float = field(default_factory=time.monotonic)
    frames: list[tuple[Event, dict[str, Any]]] = field(default_factory=list)
    task: asyncio.Task | None = None
    listeners: set[asyncio.Queue] = field(default_factory=set)
    gc_handle: asyncio.TimerHandle | None = None

    async def sink(self, event: Event, payload: dict[str, Any]) -> None:
        """`EventSink` implementation handed to the agent. Must not raise."""
        index = len(self.frames)
        self.frames.append((event, payload))
        for queue in self.listeners:
            queue.put_nowait((index, event, payload))

    async def emit(self, event: Event, **payload: Any) -> None:
        """Emit from the caller, ordered with the agent's own events."""
        await self.sink(event, payload)


_runs: dict[str, ChatRun] = {}


def get(key: str) -> ChatRun | None:
    return _runs.get(key)


def active_keys(user_id: int) -> list[str]:
    """Session ids this user has a turn running for, newest last."""
    return [
        run.key
        for run in sorted(_runs.values(), key=lambda r: r.started_at)
        if run.status == "running" and run.user_id == user_id
    ]


def start(
    key: str,
    user_id: int,
    work: Callable[[EventSink], Awaitable[None]],
) -> ChatRun:
    """
    Begin a turn for `key` and return its run.

    A key already running is returned untouched. Two concurrent turns on one
    session would interleave their frames into a single transcript, so the
    caller attaches to the live one rather than starting a rival.
    """
    existing = _runs.get(key)
    if existing is not None and existing.status == "running":
        return existing

    if existing is not None and existing.gc_handle is not None:
        existing.gc_handle.cancel()

    run = ChatRun(key=key, user_id=user_id)
    _runs[key] = run
    # `spawn`, not `ensure_future`: the turn outlives the response that
    # started it, so it must not inherit the request's thread-sensitive
    # executor — that is torn down when the response finishes, and every ORM
    # call after that would raise `CurrentThreadExecutor already quit`.
    run.task = spawn(_drive(run, work), name=f"chat-run:{key}")
    return run


async def _drive(run: ChatRun, work: Callable[[EventSink], Awaitable[None]]) -> None:
    """Run the turn, translating its outcome into the run's terminal state."""
    try:
        await work(run.sink)
    except asyncio.CancelledError:
        # `stop()` owns the terminal state for a cancelled run: it still has to
        # persist whatever was streamed before finishing the run off.
        raise
    except Exception as exc:  # noqa: BLE001 — the frame is the error report
        logger.exception("[Run] Turn failed for session %s", run.key)
        await run.emit(Event.ERROR, message=str(exc))
        finish(run, "error", str(exc))
    else:
        finish(run, "done")


def finish(run: ChatRun, status: RunStatus, error: str | None = None) -> None:
    """Mark a run terminal, release its listeners, and schedule collection."""
    if run.status != "running":
        return
    run.status = status
    run.error = error
    for queue in run.listeners:
        queue.put_nowait(_SENTINEL)
    _arm_gc(run)


def _arm_gc(run: ChatRun) -> None:
    """(Re)schedule dropping a finished run once nothing is attached to it."""
    if run.status == "running":
        return
    if run.gc_handle is not None:
        run.gc_handle.cancel()

    def collect() -> None:
        # Something is still reading it; it will be collected when the last
        # listener leaves and re-arms the timer.
        if run.listeners:
            _arm_gc(run)
            return
        if _runs.get(run.key) is run:
            del _runs[run.key]

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:  # pragma: no cover — no loop in some sync test paths
        _runs.pop(run.key, None)
        return
    run.gc_handle = loop.call_later(RETAIN_FINISHED_SECONDS, collect)


async def subscribe(
    run: ChatRun, from_index: int = 0
) -> AsyncIterator[tuple[Event, dict[str, Any]]]:
    """
    Replay this run's frames from `from_index`, then follow it live.

    The listener is registered *before* the buffer is snapshotted, so a frame
    emitted during the handover is queued rather than lost; frames already
    covered by the replay are then skipped by index. Leaving this iterator
    detaches the reader and never touches the work.
    """
    queue: asyncio.Queue = asyncio.Queue()
    run.listeners.add(queue)
    try:
        mark = len(run.frames)
        for index in range(max(from_index, 0), mark):
            yield run.frames[index]

        if run.status != "running":
            return

        while True:
            item = await queue.get()
            if item is _SENTINEL:
                return
            index, event, payload = item
            if index < mark:
                continue  # already covered by the replay above
            yield event, payload
    finally:
        run.listeners.discard(queue)
        _arm_gc(run)


async def stop(key: str, user_id: int) -> ChatRun | None:
    """
    Cancel a running turn and hand the run back for finalisation.

    The run is left in `running` so the caller can still persist the partial
    answer and emit a closing frame to whoever is attached; it calls
    `finish(run, "stopped")` when that is done.
    """
    run = _runs.get(key)
    if run is None or run.status != "running" or run.user_id != user_id:
        return None

    if run.task is not None:
        run.task.cancel()
        # Awaiting here (rather than in the cancelled task itself) is what lets
        # the caller persist afterwards without its own await being cancelled.
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await run.task
    return run


def partial_answer(run: ChatRun) -> str:
    """
    The answer text streamed so far, as the client currently has it.

    `CONTENT_RESET` retracts everything before it — that text was a preamble to
    a tool call, not part of the answer — so it resets the accumulator exactly
    as the client's live buffer does.
    """
    chunks: list[str] = []
    for event, payload in run.frames:
        if event == Event.CONTENT_RESET:
            chunks.clear()
        elif event == Event.CONTENT_CHUNK:
            chunks.append(payload.get("content", ""))
    return "".join(chunks).strip()


def clear() -> None:
    """Drop every run. For tests — production never empties the registry."""
    for run in _runs.values():
        if run.gc_handle is not None:
            run.gc_handle.cancel()
        if run.task is not None and not run.task.done():
            run.task.cancel()
    _runs.clear()
