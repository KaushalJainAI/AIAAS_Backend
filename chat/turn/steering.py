"""
Saying something to a run that is already going.

The obvious design is `interrupt()` at each iteration boundary, a resume
protocol, and a driver loop that re-invokes the graph. That is what
`interrupt()` is for — stopping to wait for an actor outside the graph — and it
is the wrong tool here, because steering is not a question. By the time the
graph looks, the message is already in the mailbox. Nothing has to wait.

So a steer is a node that drains a mailbox and returns a `HumanMessage`. There
is no interrupt, no resume protocol, no `Command`, and `run_turn` is untouched
— which also means the pause-detection seam in `run_turn` still has exactly one
reason to fire, instead of two it would have to tell apart.

Scope: in-process and single-slot, like `ChatRun` and the `MemorySaver`
checkpointer next to it. Production runs one ASGI process, so that is the whole
application; under multiple workers a steer would have to reach the worker
holding the run, and moving this to Redis is the upgrade path.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

#: Longest steer accepted. It rides in the transcript as a user message, so it
#: is charged for by the token on every subsequent turn of the run.
MAX_STEER_CHARS = 4_000


@dataclass(slots=True)
class _Slot:
    """One run's pending steer. Single-slot, last write wins."""

    message: str = ''
    replaced: int = 0
    delivered: int = 0
    extras: dict[str, Any] = field(default_factory=dict)


_slots: dict[str, _Slot] = {}


def post(key: str, message: str) -> bool:
    """
    Leave a steer for run `key`, to be picked up at its next node boundary.

    Last write wins rather than queueing. Two steers arriving between
    boundaries mean the user changed their mind — delivering both would have
    the agent act on an instruction that was already superseded, which is worse
    than dropping it. The replacement is counted so a UI can say so.
    """
    message = (message or '').strip()
    if not message:
        return False
    if len(message) > MAX_STEER_CHARS:
        message = message[:MAX_STEER_CHARS] + '\n[truncated]'

    slot = _slots.get(key)
    if slot is None:
        _slots[key] = _Slot(message=message)
        return True

    if slot.message:
        slot.replaced += 1
        logger.info('[Steer] Replaced an undelivered steer on %s', key)
    slot.message = message
    return True


def take(key: str) -> str:
    """Remove and return the pending steer for `key`, or '' if there is none.

    A plain dict lookup, which is what makes it free to call on every node
    boundary of every run whether or not anyone is steering.
    """
    slot = _slots.get(key)
    if slot is None or not slot.message:
        return ''

    message = slot.message
    slot.message = ''
    slot.delivered += 1
    return message


def pending(key: str) -> bool:
    slot = _slots.get(key)
    return bool(slot and slot.message)


def stats(key: str) -> dict[str, int]:
    slot = _slots.get(key)
    if slot is None:
        return {'delivered': 0, 'replaced': 0}
    return {'delivered': slot.delivered, 'replaced': slot.replaced}


def discard(key: str) -> None:
    """Drop a run's slot. Called when the run reaches a terminal state."""
    _slots.pop(key, None)


def clear() -> None:
    """Drop every slot. For tests — production never empties the mailbox."""
    _slots.clear()
