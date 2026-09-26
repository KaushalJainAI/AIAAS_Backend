"""
Detached coding tasks: the lead starts workers without waiting for them.

`invoke_subagent` blocks inside one tool call until every worker ends, so the
lead has no turn in which to react — it cannot steer, pause or stop a worker
mid-task. These tasks are detached instead: `launch` opens the worker's log,
takes its file leases and spawns its run, then returns a handle immediately.
The lead loops `wait_tasks` (which returns on *events*, the pattern P7's
`wait_for` uses for missions), inspects with `task_status`, and acts with
`steer_task` / `stop_task` / `revert_task`.

Sequencing is the lead's; safety is the code's. Whatever order the lead picks,
no two tasks write the same file: claims are checked against each other and
against live leases before anything starts, and the write path re-checks at
every write (`chat/tools/code.py`).

Scope: in-process, like the steering mailbox. Production runs one ASGI
process; the workers it spawns live in the same loop, so handles, events and
cancellation are plain asyncio. A second process would need Redis for this —
the same upgrade path the mailbox documents.
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

#: Workers one lead may have live at once. 3, not 8: the box is small and each
#: worker is a full model run holding a DB connection for its whole life.
MAX_CODE_WORKERS = 3

#: Tasks one `start_tasks` call may carry. Bounds model output, not the box —
#: the concurrency cap above bounds the box.
MAX_TASKS_PER_CALL = 16

#: How long a terminal record is kept for `task_status` before it is pruned.
TASK_RECORD_TTL_SECONDS = 3600

#: `wait_tasks` polls worker rows this often while nothing fires. Short
#: enough to feel live, long enough to stay out of the database's way.
WAIT_POLL_SECONDS = 2.0


@dataclass(slots=True)
class CodeTask:
    """One detached worker, from the lead's point of view."""

    handle: str
    task_id: str
    title: str
    agent_id: int | None
    agent_name: str
    label: str
    project_id: int | None
    project_name: str
    claims: tuple[str, ...] = ()
    thread_id: str = ''
    execution_id: str = ''
    status: str = 'running'
    answer: str = ''
    error: str = ''
    tokens: int = 0
    #: Wall-clock start (ms), for the panel's elapsed timers.
    started_at_ms: int = field(default_factory=lambda: int(time.time() * 1000))
    done: asyncio.Event = field(default_factory=asyncio.Event, compare=False)
    finished_at: float = 0.0


#: parent thread id -> {handle -> CodeTask}. Keyed by the lead's thread so two
#: leads never see each other's workers, and handles stay short.
_tasks: dict[str, dict[str, CodeTask]] = {}

#: parent thread id -> queued lead events (file changes, lease conflicts).
#: Drained by `wait_tasks`, which returns them the way it returns finishes:
#: one line each, and `until='any'` wakes on them. Bounded: a lead that never
#: waits must not accumulate them for ever.
_events: dict[str, list[str]] = {}

#: Events kept per lead when nothing drains them.
MAX_QUEUED_EVENTS = 50

#: parent thread id -> the lead's live sink, so detached workers can publish
#: plan-panel frames to whoever is watching the lead. Registered from the
#: lead's own tool calls (which carry its sink in context); cleared when the
#: lead's bucket is pruned empty. Never fails a run: a missing sink means
#: nobody is watching live, and `/runs` still replays from `output_data`.
_sinks: dict[str, Any] = {}

#: task handle key -> last task_update emit, for the 1-per-second throttle.
_last_emit: dict[str, float] = {}

#: Minimum seconds between task_update frames for one task.
TASK_UPDATE_THROTTLE_SECONDS = 1.0


def _bucket(parent_thread: str) -> dict[str, CodeTask]:
    return _tasks.setdefault(parent_thread or '', {})


def get(parent_thread: str, handle: str) -> CodeTask | None:
    """One task by handle, for this lead's run."""
    return _tasks.get(parent_thread or '', {}).get(handle or '')


def live_count(parent_thread: str) -> int:
    """Workers currently running or paused for this lead."""
    return sum(1 for t in _tasks.get(parent_thread or '', {}).values()
               if t.status in ('running', 'paused'))


def iter_live():
    """Every not-done task record, whatever the lead.

    The Activity live feed reads this: a worker's run is a real run with its
    own trace, and the lead's panel is not where someone looks for "what is
    running". Yields `(parent_thread, record)`; callers map execution ids to
    owners themselves, since a record carries no user.
    """
    for parent_thread, bucket in _tasks.items():
        for record in bucket.values():
            if record.status in ('running', 'paused'):
                yield parent_thread, record


def prune(parent_thread: str) -> None:
    """Drop terminal records older than the TTL."""
    import time

    bucket = _tasks.get(parent_thread or '', {})
    now = time.monotonic()
    for handle in [h for h, t in bucket.items()
                   if t.done.is_set() and t.finished_at
                   and now - t.finished_at > TASK_RECORD_TTL_SECONDS]:
        bucket.pop(handle, None)
    if not bucket:
        _tasks.pop(parent_thread or '', None)
        drop_sink(parent_thread)


def clear() -> None:
    """Drop every record. For tests."""
    _tasks.clear()
    _events.clear()
    _sinks.clear()
    _last_emit.clear()


def post_event(parent_thread: str, line: str) -> None:
    """Queue one lead event line (a file change, a lease conflict)."""
    line = (line or '').strip()
    if not line or not parent_thread:
        return
    queue = _events.setdefault(parent_thread, [])
    queue.append(line)
    while len(queue) > MAX_QUEUED_EVENTS:
        queue.pop(0)


def drain_events(parent_thread: str) -> list[str]:
    """Remove and return every queued lead event."""
    queue = _events.pop(parent_thread or '', None)
    return list(queue or [])


def set_sink(parent_thread: str, sink) -> None:
    """Remember the lead's live sink for worker frames. Never raises."""
    try:
        if parent_thread and sink is not None:
            _sinks[parent_thread] = sink
    except Exception:  # noqa: BLE001
        pass


def sink_for(parent_thread: str):
    """The lead's live sink, or None when nobody is watching live."""
    return _sinks.get(parent_thread or '')


def parent_of_execution(execution_id: str) -> str | None:
    """The lead thread whose bucket holds this execution, if any."""
    if not execution_id:
        return None
    for parent_thread, bucket in _tasks.items():
        for record in bucket.values():
            if record.execution_id == execution_id:
                return parent_thread
    return None


def finish_execution(execution_id: str, *, status: str, error: str = '') -> CodeTask | None:
    """Close whichever record holds this execution, from outside the wrapper.

    For the stop path reached over HTTP (`run_cancel`): the wrapper's own
    `CancelledError` branch does this when the task is in-process, but a
    paused worker has no task — so the endpoint closes the record itself.
    """
    parent = parent_of_execution(execution_id)
    if parent is None:
        return None
    record = None
    for candidate in _tasks[parent].values():
        if candidate.execution_id == execution_id:
            record = candidate
            break
    if record is None:
        return None
    finish(record, status=status, error=error)
    return record


def drop_sink(parent_thread: str) -> None:
    _sinks.pop(parent_thread or '', None)
    _events.pop(parent_thread or '', None)
    for key in [k for k in _last_emit if k.startswith(f'{parent_thread or ""}:')]:
        _last_emit.pop(key, None)


async def emit(parent_thread: str, event, payload: dict[str, Any],
               *, throttle_key: str = '', throttle_seconds: float = 0.0) -> None:
    """One plan-panel frame to whoever is watching the lead. Never raises.

    Throttled per key when asked (task updates at most once per second per
    task): elapsed timers live in a leaf component, but the frames still must
    not re-render the transcript — which they cannot do anyway, since the
    panel reducer is the only thing that reads them.
    """
    import time as _time

    try:
        sink = sink_for(parent_thread)
        if sink is None:
            return
        if throttle_key and throttle_seconds:
            now = _time.monotonic()
            last = _last_emit.get(throttle_key, 0.0)
            if now - last < throttle_seconds:
                return
            _last_emit[throttle_key] = now
        await sink(event, payload)
    except Exception:  # noqa: BLE001 — watching must not break the run
        logger.warning('[Tasks] Frame emit failed', exc_info=True)


def task_frame(record: CodeTask) -> dict[str, Any]:
    """The `task_update` payload for one record."""
    return {
        'task_id': record.task_id,
        'handle': record.handle,
        'label': record.label,
        'agent': record.agent_name,
        'title': record.title,
        'status': record.status,
        'execution_id': record.execution_id,
        'claims': list(record.claims),
        'tokens': record.tokens,
        'started_at_ms': record.started_at_ms,
    }


def make_handle(task_id: str, bucket: dict[str, CodeTask]) -> str:
    """`task_id` when unique, suffixed when the plan reuses one."""
    base = (task_id or 't').strip() or 't'
    if base not in bucket:
        return base
    n = 2
    while f'{base}-{n}' in bucket:
        n += 1
    return f'{base}-{n}'


def make_label(agent_name: str, bucket: dict[str, CodeTask]) -> str:
    """"Implementer #2": the name leases and approvals show the user."""
    n = sum(1 for t in bucket.values() if t.agent_name == agent_name) + 1
    return f'{agent_name} #{n}'


def new_thread_id(parent_thread: str) -> str:
    """A throwaway checkpointer key, so the worker starts with no transcript."""
    return f'code-{parent_thread}-{uuid.uuid4().hex[:8]}'


def finish(record: CodeTask, *, status: str, answer: str = '',
           error: str = '', tokens: int = 0) -> None:
    """Close a record from the worker's wrapper. Idempotent."""
    if record.done.is_set():
        return
    record.status = status
    record.answer = answer or ''
    record.error = error or ''
    record.tokens = tokens or 0
    record.finished_at = time.monotonic()
    # Set synchronously: `Event.set` needs no loop, and a record finished
    # from sync test code must read finished to every later check. What needs
    # a loop is *waiting*, which only ever happens in one.
    try:
        record.done.set()
    except RuntimeError:
        pass
