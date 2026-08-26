"""
The limits that have to exist before an agent may run other agents.

Delegation without these is unbounded in three separate directions — depth,
money, and result size — and each one compounds rather than adds. The tests
here are the reason those limits cannot be quietly relaxed.
"""
from __future__ import annotations

import asyncio

from asgiref.sync import async_to_sync
from django.test import SimpleTestCase

from agents.agent.orchestrator import (
    FANOUT_TOTAL_CHAR_LIMIT,
    MAX_DELEGATION_DEPTH,
    MAX_PARALLEL_WORKERS,
    WORKER_ANSWER_CHAR_LIMIT,
    DelegationRefused,
    FanoutResult,
    WorkerResult,
    bound_results,
    check_depth,
    divide_budget,
    run_fanout,
    worker_grants,
    worker_thread_id,
)


class DepthTests(SimpleTestCase):
    def test_a_top_level_run_may_delegate(self):
        check_depth(0)  # does not raise

    def test_delegation_stops_at_the_limit(self):
        with self.assertRaises(DelegationRefused) as caught:
            check_depth(MAX_DELEGATION_DEPTH)
        # The message has to tell the model what to do instead, or it retries.
        self.assertIn('directly', str(caught.exception))

    def test_a_worker_does_not_inherit_the_right_to_delegate(self):
        """Otherwise cost multiplies per level instead of adding.

        The depth counter bounds the damage; this removes the temptation, so a
        worker does not spend a turn planning around a tool it cannot use.
        """
        grants = worker_grants({'subAgents': True, 'webSearch': True})

        self.assertFalse(grants['subAgents'])
        self.assertTrue(grants['webSearch'])


class BudgetTests(SimpleTestCase):
    def test_no_cap_stays_no_cap(self):
        self.assertIsNone(divide_budget(None, 4))

    def test_the_cap_is_split_across_the_workers(self):
        """The race this closes: `check_guardrails` is read-then-run.

        Started with `gather`, no worker has recorded spend when the others
        check, so all N see the whole remaining cap and all N proceed.
        """
        self.assertEqual(divide_budget(400, 4), 100)

    def test_a_share_is_never_negative(self):
        self.assertEqual(divide_budget(3, 10), 0)
        self.assertEqual(divide_budget(0, 4), 0)


class BoundingTests(SimpleTestCase):
    def test_a_single_oversized_worker_is_trimmed_and_says_so(self):
        result = bound_results(FanoutResult(results=[
            WorkerResult(index=0, task='t', answer='x' * (WORKER_ANSWER_CHAR_LIMIT + 500)),
        ]))

        self.assertTrue(result.truncated)
        self.assertIn('trimmed', result.results[0].answer)

    def test_the_whole_fanout_is_capped_proportionally(self):
        """Not first-come: the answer must not depend on who replied first."""
        workers = [
            WorkerResult(index=i, task='t', answer='x' * (WORKER_ANSWER_CHAR_LIMIT - 1))
            for i in range(6)
        ]
        result = bound_results(FanoutResult(results=workers))

        total = sum(len(w.answer) for w in result.results)
        self.assertLessEqual(total, FANOUT_TOTAL_CHAR_LIMIT + 6 * 120)
        self.assertTrue(result.truncated)
        # Every worker keeps a share; none is dropped entirely.
        self.assertTrue(all(w.answer for w in result.results))

    def test_small_results_are_left_exactly_alone(self):
        result = bound_results(FanoutResult(results=[
            WorkerResult(index=0, task='t', answer='short'),
        ]))

        self.assertFalse(result.truncated)
        self.assertEqual(result.results[0].answer, 'short')


class IsolationTests(SimpleTestCase):
    def test_each_worker_gets_its_own_throwaway_thread(self):
        """Emptying `history` is not isolation — the checkpointer holds it."""
        a = worker_thread_id('parent', 0)
        b = worker_thread_id('parent', 1)

        self.assertNotEqual(a, b)
        self.assertNotIn(a, ('parent',))
        self.assertTrue(a.startswith('sub-parent-0-'))


class FanoutTests(SimpleTestCase):
    def test_results_keep_task_order_not_completion_order(self):
        """A fan-out that reshuffles per run is not reproducible."""
        async def runner(task, index, thread_id):
            # Reverse the finishing order relative to the task order.
            await asyncio.sleep(0.02 * (3 - index))
            return WorkerResult(index=index, task=task, answer=f'answer-{index}')

        result = async_to_sync(run_fanout)(
            ['a', 'b', 'c'], runner=runner, parent_thread='p',
        )

        self.assertEqual([w.answer for w in result.results],
                         ['answer-0', 'answer-1', 'answer-2'])

    def test_workers_really_do_run_in_parallel(self):
        async def runner(task, index, thread_id):
            await asyncio.sleep(0.15)
            return WorkerResult(index=index, task=task, answer='done')

        async def timed():
            start = asyncio.get_running_loop().time()
            await run_fanout(['a', 'b', 'c', 'd'], runner=runner, parent_thread='p')
            return asyncio.get_running_loop().time() - start

        elapsed = async_to_sync(timed)()
        # Four 0.15s workers: ~0.15s in parallel, ~0.6s sequential.
        self.assertLess(elapsed, 0.45)

    def test_one_worker_failing_does_not_lose_the_others(self):
        async def runner(task, index, thread_id):
            if index == 1:
                raise RuntimeError('provider exploded')
            return WorkerResult(index=index, task=task, answer='fine')

        result = async_to_sync(run_fanout)(
            ['a', 'b', 'c'], runner=runner, parent_thread='p',
        )

        self.assertEqual(len(result.results), 3)
        self.assertTrue(result.results[1].failed)
        self.assertEqual(result.as_dict()['succeeded'], 2)
        self.assertEqual(result.as_dict()['failed'], 1)

    def test_a_failure_is_structured_never_prose(self):
        """A failure the model reads as an answer becomes a fact it cites."""
        async def runner(task, index, thread_id):
            raise RuntimeError('no credential for openai')

        result = async_to_sync(run_fanout)(['a'], runner=runner, parent_thread='p')
        payload = result.as_dict()['workers'][0]

        self.assertTrue(payload['failed'])
        self.assertIn('no credential', payload['error'])
        self.assertNotIn('answer', payload)

    def test_concurrency_is_capped(self):
        live = 0
        peak = 0

        async def runner(task, index, thread_id):
            nonlocal live, peak
            live += 1
            peak = max(peak, live)
            await asyncio.sleep(0.02)
            live -= 1
            return WorkerResult(index=index, task=task, answer='ok')

        async_to_sync(run_fanout)(
            [str(i) for i in range(20)], runner=runner, parent_thread='p',
        )

        self.assertLessEqual(peak, MAX_PARALLEL_WORKERS)

    def test_a_requested_parallelism_is_honoured_within_the_cap(self):
        live = 0
        peak = 0

        async def runner(task, index, thread_id):
            nonlocal live, peak
            live += 1
            peak = max(peak, live)
            await asyncio.sleep(0.02)
            live -= 1
            return WorkerResult(index=index, task=task, answer='ok')

        async_to_sync(run_fanout)(
            [str(i) for i in range(10)], runner=runner, parent_thread='p', parallel=2,
        )

        self.assertLessEqual(peak, 2)

    def test_cancelling_the_parent_cancels_every_worker(self):
        """Otherwise stopping one run leaves eight going."""
        started = asyncio.Event()
        cancelled: list[int] = []

        async def runner(task, index, thread_id):
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.append(index)
                raise

        async def drive():
            job = asyncio.ensure_future(
                run_fanout(['a', 'b', 'c'], runner=runner, parent_thread='p')
            )
            await started.wait()
            await asyncio.sleep(0.01)
            job.cancel()
            try:
                await job
            except asyncio.CancelledError:
                pass

        async_to_sync(drive)()
        self.assertEqual(sorted(cancelled), [0, 1, 2])
