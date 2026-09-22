"""
Per-run read set for the stale-write guard.

`ws_read` records the file's sha256 under the run's thread id; `ws_edit` and
`ws_apply_patch` refuse when the file changed since that run last read it.
In-process, like the steering mailbox: production runs one ASGI process, and
the guard is enforced at write time in the same process that served the read.

This is the enforcement half. `chat/turn/agent.py::_on_ws_read` mirrors the
entry into `meta['reads']` so the record survives in the run's own metadata
and the UI can show it — but the refusal reads this module, never the graph
state, so curation can never lift the guard by folding the transcript.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

#: thread id -> {normalised path -> sha256 hex}. Bounded per thread by the
#: caller; entries die with the run (`discard_thread` on every terminal path).
_reads: dict[str, dict[str, str]] = {}


def record(thread: str, path: str, digest: str) -> None:
    """Remember that `thread` saw `path` at `digest`."""
    thread = (thread or '').strip()
    path = (path or '').strip()
    if not thread or not path:
        return
    _reads.setdefault(thread, {})[path] = digest or ''


def last_read(thread: str, path: str) -> str | None:
    """The digest `thread` last saw for `path`, or None if it never read it."""
    return _reads.get(thread or '', {}).get((path or '').strip())


def discard_thread(thread: str) -> None:
    """Drop a run's read set. Called on every terminal path."""
    if thread:
        _reads.pop(thread, None)


def snapshot(thread: str) -> dict[str, str]:
    """A copy of one run's read set, for metadata mirroring."""
    return dict(_reads.get(thread or '', {}))


def clear() -> None:
    """Drop every read set. For tests."""
    _reads.clear()
