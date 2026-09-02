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

The slot carries a second thing for the same reason: the run's autonomy level.
Both are messages from a watching human into a loop that is already going, both
have to land between node boundaries, and both would otherwise need their own
transport — so they share this one. They differ in one way that matters, and it
is why `autonomy` is not simply an `extras` key: a steer is *drained* when
delivered, because it is an instruction to act on once, while a mode is a
standing answer that has to survive every subsequent batch.

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
    #: An autonomy level the user chose *while the run was going*, or ''.
    #:
    #: Unlike `message` this is not drained on read. A steer is an instruction
    #: to act on once; a mode is a standing answer to "must I ask?", and one
    #: that evaporated after the next tool call would leave the user approving
    #: again three seconds after saying "stop asking".
    autonomy: str = ''


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


#: Levels a run can be switched to while it is going. `plan` is deliberately
#: absent: which tools exist is decided once, when the toolbox is built and
#: handed to the frozen `TurnContext`, so "switch to plan" mid-run could only
#: ever gate the mutating tools rather than withdraw them — which is `review`
#: wearing the wrong name. A run that should not have been allowed to act is
#: stopped, not relabelled.
SWITCHABLE = frozenset({'review', 'ask', 'auto', 'full'})


def set_autonomy(key: str, level: str) -> bool:
    """
    Change how much run `key` asks, from now on.

    This is the mid-run half of the autonomy setting, and it exists because the
    other half is build-time configuration: `SubAgent.guardrails['autonomy']`
    is chosen before anyone knows what the run will actually do. A person
    watching a run pause for the sixth time on the same recycled file write
    could otherwise only stop it and edit the agent — so in practice they set
    `full` once, in advance, and stopped being asked about anything ever.

    Takes effect at the next `tools_node` pass, which reads it per batch rather
    than capturing it. Deliberately *not* retroactive: a call already paused
    stays paused, because the loosened setting arrived after the question and
    answering a question the user did not answer is how consent gets laundered.
    """
    if level not in SWITCHABLE:
        return False
    slot = _slots.get(key)
    if slot is None:
        _slots[key] = _Slot(autonomy=level)
    else:
        slot.autonomy = level
    logger.info('[Steer] Autonomy for %s set to %s', key, level)
    return True


def autonomy(key: str) -> str:
    """The mid-run autonomy override for `key`, or '' if the user set none.

    A plain dict lookup, so `tools_node` can afford to consult it on every
    batch of every run — which is what makes the change take effect without a
    resume protocol.
    """
    slot = _slots.get(key)
    return slot.autonomy if slot else ''


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
