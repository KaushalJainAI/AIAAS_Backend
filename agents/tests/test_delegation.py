"""
The limits that have to exist before an agent may run other agents.

Delegation without these is unbounded in three separate directions — depth,
money, and result size — and each one compounds rather than adds. The tests
here are the reason those limits cannot be quietly relaxed.
"""
from __future__ import annotations

import asyncio

from unittest.mock import patch

from asgiref.sync import async_to_sync
from django.contrib.auth.models import User
from django.test import SimpleTestCase, TestCase

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
    """What a fan-out hands back, and what happens to what it cuts.

    `bound_results` became async when trimming stopped being lossy: the full
    answer is archived so the parent can still fetch it. Called with no
    context — every case here but the last — it degrades to the old behaviour
    and says the text was not kept, rather than naming an id nobody wrote.
    """

    def _bound(self, workers, context=None):
        return async_to_sync(bound_results)(
            FanoutResult(results=workers), context,
        )

    def test_a_single_oversized_worker_is_trimmed_and_says_so(self):
        result = self._bound([
            WorkerResult(index=0, task='t',
                         answer='x' * (WORKER_ANSWER_CHAR_LIMIT + 500)),
        ])

        self.assertTrue(result.truncated)
        self.assertIn('trimmed', result.results[0].answer)

    def test_the_whole_fanout_is_capped_proportionally(self):
        """Not first-come: the answer must not depend on who replied first."""
        workers = [
            WorkerResult(index=i, task='t', answer='x' * (WORKER_ANSWER_CHAR_LIMIT - 1))
            for i in range(6)
        ]
        result = self._bound(workers)

        total = sum(len(w.answer) for w in result.results)
        self.assertLessEqual(total, FANOUT_TOTAL_CHAR_LIMIT + 6 * 200)
        self.assertTrue(result.truncated)
        # Every worker keeps a share; none is dropped entirely.
        self.assertTrue(all(w.answer for w in result.results))

    def test_small_results_are_left_exactly_alone(self):
        result = self._bound([WorkerResult(index=0, task='t', answer='short')])

        self.assertFalse(result.truncated)
        self.assertEqual(result.results[0].answer, 'short')

    def test_without_a_context_the_notice_admits_the_text_is_gone(self):
        """Naming an id nobody wrote is worse than admitting the loss — the
        model would spend a turn fetching something that does not exist."""
        result = self._bound([
            WorkerResult(index=0, task='t',
                         answer='x' * (WORKER_ANSWER_CHAR_LIMIT + 10)),
        ])
        self.assertIn('not kept', result.results[0].answer)
        self.assertNotIn('read_tool_output', result.results[0].answer)


class TrimmedAnswersStayReachableTests(TestCase):
    """A trimmed worker answer used to be lost outright.

    The parent had paid for a whole worker run and could see only the first N
    characters of what it bought; the only way to the rest was to delegate the
    same task again. It is now archived through the same store an oversized
    tool result uses, so `read_tool_output` fetches it — which is the promise
    that tool already makes everywhere else.
    """

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user('fanout', 'f@example.com', 'x')

    def test_the_full_answer_is_stored_and_the_notice_names_the_id(self):
        from chat.models import ToolOutput

        full = 'y' * (WORKER_ANSWER_CHAR_LIMIT + 4_000)
        context = {'user_id': self.user.id, 'session_id': 'run-42',
                   'turn_id': 't1'}

        result = async_to_sync(bound_results)(
            FanoutResult(results=[WorkerResult(index=0, task='t', answer=full)]),
            context,
        )

        answer = result.results[0].answer
        self.assertIn('read_tool_output', answer)

        row = ToolOutput.objects.get(user=self.user, session_key='run-42')
        self.assertEqual(row.total_chars, len(full))
        self.assertIn(str(row.id), answer)

    def test_a_failed_archive_still_tells_the_truth(self):
        """Best-effort, like every other archive here: failing to store must
        not fail the fan-out, and must not claim an id either."""
        async def _no_storage(*args, **kwargs):
            return None

        with patch('chat.tools.tool_output.spill', _no_storage):
            result = async_to_sync(bound_results)(
                FanoutResult(results=[
                    WorkerResult(index=0, task='t',
                                 answer='z' * (WORKER_ANSWER_CHAR_LIMIT + 10)),
                ]),
                {'user_id': self.user.id, 'session_id': 'run-43'},
            )

        answer = result.results[0].answer
        self.assertIn('could not be kept', answer)
        self.assertNotIn('read_tool_output', answer)


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
