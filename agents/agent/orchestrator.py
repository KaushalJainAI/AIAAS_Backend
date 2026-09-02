"""
One agent delegating to others.

There is no orchestrator *kind*. An agent that fans out is a `SubAgent` holding
the `subAgents` grant, so delegation goes through the same grant check as web
search does. This module is what that grant unlocks: run N workers, bound what
comes back, and refuse the cases that would otherwise be unbounded.

Three things make delegation safe rather than merely possible, and all three
are the kind of limit that has to exist before the feature ships, not after:

**Depth.** A worker inherits its parent's toolbox. If that toolbox contains
`subAgents`, the worker can delegate too, and the cost is multiplicative rather
than additive. `TurnContext.depth` counts, and `MAX_DELEGATION_DEPTH` stops it.
Workers are also handed a *narrowed* toolbox — never the parent's — so a
delegating agent does not silently hand its own delegation rights down.

**Budget.** `check_guardrails` is a read-then-run aggregate: it asks what has
been spent and then starts a run. Sequentially that is fine. With `gather`,
every worker asks before any of them has recorded anything, so N siblings all
see the same "under the cap" answer and all proceed. The fan-out therefore
reserves its own budget up front and divides it, rather than letting each
worker take the whole remaining cap.

**Isolation.** Each worker gets a throwaway `thread_id`. The checkpointer — not
the `history` list — is what actually holds a conversation, so a worker sharing
its parent's thread would see the parent's transcript no matter how empty its
own history was.
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

#: How many agents deep a chain may go. 2 means a user-started agent may
#: delegate, and its workers may not. Raising this multiplies cost per level.
MAX_DELEGATION_DEPTH = 2

#: Workers one fan-out may run at once. The cap is on concurrency rather than
#: total count so a long list still completes; it just does not open forty
#: provider connections at the same moment.
MAX_PARALLEL_WORKERS = 8

#: Per-worker answer budget, and the ceiling on a whole fan-out. A worker that
#: returns more than its share is trimmed with a notice, never silently.
WORKER_ANSWER_CHAR_LIMIT = 20_000
FANOUT_TOTAL_CHAR_LIMIT = 60_000

#: What the parent may send *down*. Results were bounded from the start and
#: instructions were not, which is the wrong way round to leave it: the answer
#: comes back to one window, while a task is copied into every worker's window
#: and paid for once per worker. A parent that restates a long briefing into six
#: tasks pays for it six times.
#:
#: Refused rather than truncated. A trimmed instruction is a worker confidently
#: doing the wrong job, and the parent is a model that can be told to shorten
#: and try again — which is not true of a tool result arriving from outside.
DELEGATION_TASK_CHAR_LIMIT = 8_000

#: Shared context sent to every worker, once each. Larger than one task because
#: it replaces the duplication rather than adding to it: the alternative is the
#: same background pasted into all N tasks.
DELEGATION_BRIEFING_CHAR_LIMIT = 16_000

#: Workers one fan-out may start. `MAX_PARALLEL_WORKERS` caps how many run at
#: the same moment, which bounds connections and nothing else — fifty tasks
#: still meant fifty full model runs, eight at a time. Spend is divided N ways
#: so the money was never the exposure; time and the parent's own window were.
MAX_WORKERS_PER_FANOUT = 16


def check_delegation_payload(tasks: list[str], briefing: str = "") -> None:
    """Refuse a fan-out whose instructions are too large to be sensible.

    Raises `DelegationRefused` with a message written for the model that wrote
    the tasks: it says which task, how long it is, and what to do instead.
    """
    if len(tasks) > MAX_WORKERS_PER_FANOUT:
        raise DelegationRefused(
            f"{len(tasks)} workers is more than the {MAX_WORKERS_PER_FANOUT} one "
            f"fan-out may start. Group the work into fewer, larger tasks."
        )

    if len(briefing) > DELEGATION_BRIEFING_CHAR_LIMIT:
        raise DelegationRefused(
            f"The briefing is {len(briefing):,} characters, over the "
            f"{DELEGATION_BRIEFING_CHAR_LIMIT:,} limit. Send the workers what "
            f"they need to act on, not everything you have read."
        )

    for index, task in enumerate(tasks):
        if len(task) > DELEGATION_TASK_CHAR_LIMIT:
            raise DelegationRefused(
                f"Task {index + 1} is {len(task):,} characters, over the "
                f"{DELEGATION_TASK_CHAR_LIMIT:,} limit. Put shared background in "
                f"`briefing` — it is sent to every worker once — and keep each "
                f"task to what that worker alone must do."
            )


class DelegationRefused(Exception):
    """The delegation was rejected before any model call."""


@dataclass(slots=True)
class WorkerResult:
    """One worker's outcome. Failures are structured, never prose."""

    index: int
    task: str
    answer: str = ''
    failed: bool = False
    error: str = ''
    tokens: int = 0
    execution_id: str = ''

    def as_dict(self) -> dict[str, Any]:
        if self.failed:
            return {'worker': self.index, 'task': self.task,
                    'failed': True, 'error': self.error}
        return {'worker': self.index, 'task': self.task,
                'answer': self.answer, 'tokens': self.tokens,
                'execution_id': self.execution_id}


@dataclass(slots=True)
class FanoutResult:
    results: list[WorkerResult] = field(default_factory=list)
    truncated: bool = False

    @property
    def tokens(self) -> int:
        return sum(r.tokens for r in self.results)

    def as_dict(self) -> dict[str, Any]:
        return {
            'workers': [r.as_dict() for r in self.results],
            'succeeded': sum(1 for r in self.results if not r.failed),
            'failed': sum(1 for r in self.results if r.failed),
            'truncated': self.truncated,
        }


def check_depth(depth: int) -> None:
    """Refuse a delegation that is already as deep as we allow."""
    if depth >= MAX_DELEGATION_DEPTH:
        raise DelegationRefused(
            f'Delegation is limited to {MAX_DELEGATION_DEPTH} levels and this '
            f'run is already at level {depth}. Do this work directly instead '
            f'of asking another agent to do it.'
        )


def worker_grants(parent_grants: dict[str, Any]) -> dict[str, Any]:
    """The toolbox a worker inherits: its parent's, minus the right to delegate.

    Narrowing here rather than relying on the depth counter alone is belt and
    braces on purpose — the counter bounds the damage, this removes the
    temptation. A worker that cannot see `invoke_subagent` will not spend a
    turn planning around it.
    """
    grants = {k: bool(v) for k, v in (parent_grants or {}).items()}
    grants['subAgents'] = False
    return grants


def divide_budget(remaining: int | None, workers: int) -> int | None:
    """Split what is left of the spend cap across the workers about to start.

    Returns None when there is no cap. Otherwise every worker gets an equal
    share, floored at zero — the point is that N concurrent workers cannot each
    spend the whole remaining budget because none of them has recorded any
    spend yet when the others check.
    """
    if remaining is None:
        return None
    if workers <= 0:
        return 0
    return max(0, remaining // workers)


def bound_results(result: FanoutResult) -> FanoutResult:
    """Cap each worker, then the whole corpus, naming anything trimmed.

    Proportional rather than first-come: truncating by arrival order would make
    the fan-out's answer depend on which provider happened to respond first,
    which is not a property anyone wants their research to have.
    """
    for worker in result.results:
        if len(worker.answer) > WORKER_ANSWER_CHAR_LIMIT:
            worker.answer = (
                worker.answer[:WORKER_ANSWER_CHAR_LIMIT]
                + f'\n\n[trimmed to {WORKER_ANSWER_CHAR_LIMIT} characters]'
            )
            result.truncated = True

    total = sum(len(w.answer) for w in result.results)
    if total <= FANOUT_TOTAL_CHAR_LIMIT or not result.results:
        return result

    share = FANOUT_TOTAL_CHAR_LIMIT // len(result.results)
    for worker in result.results:
        if len(worker.answer) > share:
            worker.answer = (
                worker.answer[:share]
                + f'\n\n[trimmed to a {share}-character share of the fan-out budget]'
            )
    result.truncated = True
    return result


def worker_thread_id(parent_thread: str, index: int) -> str:
    """A throwaway checkpointer key, so the worker starts with no transcript.

    Emptying `history` is not enough and never was: the checkpointer holds its
    own copy of the conversation, keyed by thread id. A worker reusing its
    parent's key inherits everything the parent has said.
    """
    return f'sub-{parent_thread}-{index}-{uuid.uuid4().hex[:8]}'


async def run_fanout(
    tasks: list[str],
    *,
    runner,
    parent_thread: str,
    parallel: int | None = None,
) -> FanoutResult:
    """
    Run every task, bounded and in order, and hand back structured outcomes.

    `runner` is `async (task, index, thread_id) -> WorkerResult`; the caller
    supplies it so this function never has to know how a run is started. That
    is what lets the same code serve a saved orchestrator agent and an ad-hoc
    fan-out from a tool.

    Results keep the order of `tasks`, not the order they finished in — a
    fan-out whose output reshuffles per run is not reproducible, and the model
    reading it cannot refer to "the third one" between turns.

    A cancel propagates: every outstanding worker is cancelled before this
    returns, so stopping the parent does not leave eight runs going.
    """
    limit = max(1, min(parallel or MAX_PARALLEL_WORKERS, MAX_PARALLEL_WORKERS))
    gate = asyncio.Semaphore(limit)

    async def one(index: int, task: str) -> WorkerResult:
        async with gate:
            try:
                return await runner(task, index, worker_thread_id(parent_thread, index))
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                # Structured, never prose. A worker failure the model reads as
                # an ordinary answer becomes a fact it will cite later.
                logger.exception('[Fanout] Worker %s failed', index)
                return WorkerResult(index=index, task=task, failed=True,
                                    error=str(exc))

    jobs = [asyncio.ensure_future(one(i, t)) for i, t in enumerate(tasks)]
    try:
        gathered = await asyncio.gather(*jobs)
    except asyncio.CancelledError:
        for job in jobs:
            job.cancel()
        # Let them observe the cancel before the parent unwinds, so no worker
        # is left running against a run that no longer exists.
        await asyncio.gather(*jobs, return_exceptions=True)
        raise

    return bound_results(FanoutResult(results=list(gathered)))
