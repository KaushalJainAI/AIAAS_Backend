"""How much wall-clock time one run may spend, and what a worker inherits.

The builder used to offer CPUs and Memory (MB). Neither was ever read, and the
panel said so — because neither *could* be: `sandbox/safe_execution.py` runs
user code with `exec` on a worker thread inside the Django process, where there
is no cgroup to hang a CPU quota off and no way to cap one thread's RSS. Only
the wasm engine could enforce a memory ceiling, and it has no callers.

Meanwhile the thing a run genuinely contends for had no limit at all. A run is
almost entirely *waiting* — on a provider, on a tool, on an MCP subprocess —
and while it waits it holds an event-loop slot, a checkpointer's super-steps, a
DB connection and a socket. Forty iterations against a slow provider is an hour
of that, and nothing anywhere would have stopped it.

So the resource is time, and this is where it is counted.

**Absolute, not a duration.** A `Deadline` is an instant on the monotonic
clock, computed once when the run starts. Everything downstream asks it how
much is left. A duration passed down would restart at every hop, which is
exactly how a bound stops bounding anything.

**Shared by workers, not divided among them.** `divide_budget` splits the spend
cap N ways because N concurrent workers' spend adds up. Wall-clock does not:
eight workers running for a minute cost one minute, not eight. Dividing time
would therefore make each worker useless without protecting anything. What does
need protecting is the parent's own last turn — so a fan-out gets a *share* of
the parent's remaining time (`WORKER_DEADLINE_SHARE`) and the parent keeps the
rest to read the answers it paid for.
"""
from __future__ import annotations

import time
from dataclasses import dataclass

from workflow_backend.thresholds import (
    DEFAULT_RUN_SECONDS,
    MAX_RUN_SECONDS,
    MIN_RUN_SECONDS,
    MIN_WORKER_SECONDS,
    RUN_WRAPUP_SECONDS,
    WORKER_DEADLINE_SHARE,
)


class OutOfTime(Exception):
    """There is not enough of the parent's budget left to start something."""


def clamp_run_seconds(value) -> int:
    """A user-supplied limit, coerced into the range the runtime will honour.

    Anything unparseable becomes the default rather than an error: this is read
    on the hot path from a JSON column that predates the field, so every agent
    saved before it existed has no value at all and must still run.
    """
    try:
        seconds = int(value)
    except (TypeError, ValueError):
        return DEFAULT_RUN_SECONDS
    return max(MIN_RUN_SECONDS, min(seconds, MAX_RUN_SECONDS))


def limit_for(agent) -> int:
    """The configured ceiling for one run of `agent`, in seconds."""
    guards = getattr(agent, 'guardrails', None) or {}
    if 'maxRunSeconds' not in guards:
        return DEFAULT_RUN_SECONDS
    return clamp_run_seconds(guards.get('maxRunSeconds'))


@dataclass(frozen=True, slots=True)
class Deadline:
    """An instant by which a run must be finished.

    Frozen because `TurnContext` is frozen and this rides on it: a deadline that
    could be pushed back from inside the run it bounds is not a deadline.
    """

    #: `time.monotonic()` instant. Monotonic rather than wall-clock so a clock
    #: adjustment mid-run cannot extend or expire it.
    at: float
    #: What was asked for, in seconds. Carried only so a message can say "10
    #: minutes" instead of naming an instant nobody can interpret.
    limit: int

    @classmethod
    def after(cls, seconds: int) -> 'Deadline':
        seconds = max(0, int(seconds))
        return cls(at=time.monotonic() + seconds, limit=seconds)

    @classmethod
    def for_agent(cls, agent) -> 'Deadline':
        return cls.after(limit_for(agent))

    def remaining(self) -> float:
        """Seconds left. Never negative — callers compare, they do not add."""
        return max(0.0, self.at - time.monotonic())

    @property
    def expired(self) -> bool:
        return self.remaining() <= 0

    @property
    def wrapping_up(self) -> bool:
        """True once there is only enough left to write an answer.

        This is what the agent loop reads. Stopping *here* rather than at zero
        is the difference between a run that returns what it found and one that
        is killed mid-call having paid for everything and produced nothing.
        """
        return self.remaining() <= RUN_WRAPUP_SECONDS

    def share_for_workers(self) -> float:
        """Seconds a fan-out may take, leaving the parent its own last turn."""
        return self.remaining() * WORKER_DEADLINE_SHARE

    def child(self, limit: int | None = None) -> 'Deadline':
        """The deadline a delegated worker runs under.

        `min` of the worker's own configured limit and the parent's share, so a
        worker configured for an hour inside a parent with four minutes left
        gets the four minutes — the whole point being that a subagent cannot
        outlive, or outspend in time, the run that asked for it.

        Raises `OutOfTime` rather than returning an already-dead deadline: N
        workers that each fail on their first model call is a worse answer than
        one refusal the model can act on.
        """
        share = self.share_for_workers()
        if share < MIN_WORKER_SECONDS:
            raise OutOfTime(
                f'Only {int(self.remaining())}s of this run\'s '
                f'{self.limit}s budget remain — not enough to delegate. '
                f'Answer with what you already have.'
            )
        seconds = share if limit is None else min(float(limit), share)
        return Deadline(at=time.monotonic() + seconds, limit=int(seconds))


def describe(deadline: 'Deadline | None') -> str:
    """How a run that ran out of time explains itself to whoever reads the log."""
    if deadline is None:
        return 'The run exceeded its time limit.'
    minutes = deadline.limit / 60
    shown = f'{minutes:.0f} minute{"s" if minutes >= 2 else ""}' \
        if minutes >= 1 else f'{deadline.limit} seconds'
    return (
        f'The run reached its {shown} time limit and was stopped. '
        f'Raise "Time limit" in the agent\'s settings if it needs longer.'
    )
