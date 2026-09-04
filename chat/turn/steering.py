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

Scope: in-process, like `ChatRun` and the `MemorySaver` checkpointer next to
it. Production runs one ASGI process, so that is the whole application; under
multiple workers a steer would have to reach the worker holding the run, and
moving this to Redis is the upgrade path.

**A queue, not a slot (2026-09-04).** This began as one slot, last write wins,
on the reasoning that two steers between boundaries mean the user changed their
mind. That is true of a correction and false of everything else. The way people
actually steer a working agent is additive — "also check pricing", then "and
the changelog" — and dropping all but the last turned three instructions into
one, with no error and no trace. A superseding correction is still expressible;
it is just the user's own words that say so, which is the only place that
distinction was ever legible. What is bounded instead is the total, because a
steer rides in the transcript as a user message and is re-billed on every
subsequent turn of the run.
"""
from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

#: Longest single steer accepted. It rides in the transcript as a user message,
#: so it is charged for by the token on every subsequent turn of the run.
MAX_STEER_CHARS = 4_000

#: Steers that may be waiting at once. Past this the *oldest* is dropped, not
#: the newest: the queue exists so that later instructions arrive, and refusing
#: new ones once it is full would make a full mailbox silently ignore the user.
MAX_QUEUED_STEERS = 8

#: Total characters one delivery may carry. Separate from `MAX_STEER_CHARS`
#: because eight legal steers are still eight times the cost, and the whole
#: batch lands in one message that every later turn pays for.
MAX_BATCH_CHARS = 8_000


@dataclass(slots=True)
class _Slot:
    """One run's mailbox: a FIFO of undelivered steers, plus standing state."""

    messages: deque = field(default_factory=deque)
    #: Steers discarded because the queue was full when they arrived.
    dropped: int = 0
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

    Queued in arrival order, and every one of them is delivered. Someone
    steering a working agent is usually adding to the brief rather than
    replacing it, and the previous last-write-wins behaviour turned "check
    pricing", "and the changelog", "and their docs" into one instruction while
    reporting success for all three.

    Order is FIFO because these are instructions from one person typing: a
    later one may well refine an earlier one, and the model can only weigh them
    correctly if it reads them the way they were written.
    """
    message = (message or '').strip()
    if not message:
        return False
    if len(message) > MAX_STEER_CHARS:
        message = message[:MAX_STEER_CHARS] + '\n[truncated]'

    slot = _slots.get(key)
    if slot is None:
        slot = _slots[key] = _Slot()

    slot.messages.append(message)
    while len(slot.messages) > MAX_QUEUED_STEERS:
        # Oldest out, not newest refused. A mailbox this full means the run is
        # not reaching a boundary, and in that case the user's most recent
        # instruction is the one most worth keeping.
        slot.messages.popleft()
        slot.dropped += 1
        logger.warning('[Steer] Queue full on %s; dropped the oldest steer', key)
    return True


def take(key: str) -> str:
    """Remove and return every pending steer for `key`, or '' if there are none.

    Drained together and joined into one string, because the caller turns this
    into a single `HumanMessage`. Returning several would break the invariant
    `_split_transcript` relies on — it peels *one* trailing human message off as
    the turn's prompt, so extra ones would sit in the transcript unread as the
    prompt while still being paid for.

    Numbered when there is more than one, so a model cannot read three separate
    instructions as one run-on sentence.
    """
    slot = _slots.get(key)
    if slot is None or not slot.messages:
        return ''

    items = list(slot.messages)
    slot.messages.clear()
    slot.delivered += len(items)

    if len(items) == 1:
        body = items[0]
    else:
        body = '\n'.join(f'{i}. {text}' for i, text in enumerate(items, 1))
        body = (
            f'{len(items)} messages arrived while you were working. Take them '
            f'together, in order:\n{body}'
        )

    if len(body) > MAX_BATCH_CHARS:
        # The tail, not the head: when a batch has to be cut, the instructions
        # the user sent most recently are the ones they are waiting on.
        body = '[earlier queued messages dropped to fit]\n' + body[-MAX_BATCH_CHARS:]
    return body


def pending(key: str) -> bool:
    slot = _slots.get(key)
    return bool(slot and slot.messages)


def queued(key: str) -> int:
    """How many steers are waiting, so a UI can show a pending count."""
    slot = _slots.get(key)
    return len(slot.messages) if slot else 0


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
    """Delivery counters for one run, for the API that accepts a steer.

    `dropped` replaced `replaced` when the slot became a queue: nothing is
    superseded any more, so the only loss left to report is a mailbox that
    overflowed. It is reported rather than silent because a dropped
    instruction the user believes was accepted is the failure this whole
    module exists to avoid.
    """
    slot = _slots.get(key)
    if slot is None:
        return {'delivered': 0, 'dropped': 0, 'queued': 0}
    return {
        'delivered': slot.delivered,
        'dropped': slot.dropped,
        'queued': len(slot.messages),
    }


def discard(key: str) -> None:
    """Drop a run's slot. Called when the run reaches a terminal state."""
    _slots.pop(key, None)


def clear() -> None:
    """Drop every slot. For tests — production never empties the mailbox."""
    _slots.clear()
