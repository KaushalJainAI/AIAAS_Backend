"""
Sweep tests.

The agent runtime is stubbed at `agents.agent.runtime.run_agent` — the seam the
runner imports through — so these exercise the sweep's own behaviour: what it
records, what it stops for, and what it hands to a person.
"""
from unittest.mock import patch

from asgiref.sync import async_to_sync, sync_to_async
from django.contrib.auth.models import User
from django.test import TestCase

from agents.agent.runtime import AgentRun, AgentRunRefused
from agents.models import SubAgent
from eval import runner
from eval.models import EvalCase, EvalResult, EvalRun, EvalSuite


def agent_run(answer='Paris', **kwargs):
    defaults = dict(
        execution_id='00000000-0000-0000-0000-000000000000',
        answer=answer, thinking='', tool_trace=[], tokens=100,
        awaiting_approval=False, unserved_grants=(), duration_ms=250,
    )
    return AgentRun(**{**defaults, **kwargs})


def stub(*returns, raises=None):
    """A `run_agent` stand-in that never touches a provider."""
    calls = []

    async def fake(agent, goal, **kwargs):
        calls.append(goal)
        if raises is not None:
            raise raises
        return returns[min(len(calls) - 1, len(returns) - 1)]

    fake.calls = calls
    return fake


class SweepTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user('sweeper', 's@example.com', 'pw')
        self.agent = SubAgent.objects.create(user=self.user, name='Geo')
        self.suite = EvalSuite.objects.create(
            user=self.user, name='Capitals', subagent=self.agent,
            supervision='failures', pass_threshold=0.5, concurrency=2,
        )

    def case(self, goal='Capital of France?', **kwargs):
        defaults = dict(
            suite=self.suite, goal=goal,
            graders=[{'type': 'contains', 'value': 'paris', 'weight': 1.0}],
        )
        return EvalCase.objects.create(**{**defaults, **kwargs})

    def sweep(self, fake):
        run = async_to_sync(runner.open_run)(self.suite, self.agent, self.user, '')
        with patch('agents.agent.runtime.run_agent', fake):
            async_to_sync(runner.sweep)(run, self.suite, self.agent, self.user)
        run.refresh_from_db()
        return run

    def test_a_passing_sweep_completes_and_scores(self):
        self.case()
        run = self.sweep(stub(agent_run('The capital is Paris.')))

        self.assertEqual(run.status, 'completed')
        self.assertEqual(run.total_cases, 1)
        self.assertEqual(run.passed_count, 1)
        self.assertEqual(run.score, 1.0)
        self.assertTrue(run.passed)
        self.assertEqual(run.tokens_used, 100)
        self.assertIsNotNone(run.duration_ms)

    def test_a_failing_case_is_queued_under_the_failures_policy(self):
        self.case()
        run = self.sweep(stub(agent_run('The capital is Berlin.')))

        self.assertEqual(run.status, 'awaiting_review')
        self.assertEqual(run.pending_review_count, 1)
        result = run.results.get()
        self.assertFalse(result.auto_passed)
        self.assertEqual(result.review_state, 'pending')
        self.assertIn('failed', result.review_reason)

    def test_the_run_records_what_it_was_configured_as(self):
        self.case()
        run = self.sweep(stub(agent_run()))
        # Pinned at open time; this is what makes a score comparable later.
        self.assertIsNotNone(run.revision_id)
        self.assertEqual(run.revision.subagent_id, self.agent.id)

    def test_input_data_reaches_the_agent_as_labelled_json(self):
        self.case(goal='Summarise', input_data={'ticket': 42})
        fake = stub(agent_run())
        self.sweep(fake)

        goal = fake.calls[0]
        self.assertIn('Summarise', goal)
        self.assertIn('INPUT DATA (JSON)', goal)
        self.assertIn('"ticket": 42', goal)

    def test_inactive_cases_are_not_swept(self):
        self.case()
        self.case(goal='skipped one', is_active=False)
        fake = stub(agent_run())
        run = self.sweep(fake)

        self.assertEqual(len(fake.calls), 1)
        self.assertEqual(run.total_cases, 1)

    def test_a_case_with_no_graders_goes_to_a_person(self):
        self.case(graders=[])
        run = self.sweep(stub(agent_run()))

        result = run.results.get()
        self.assertIsNone(result.auto_passed)
        self.assertEqual(result.review_state, 'pending')
        self.assertEqual(run.status, 'awaiting_review')

    def test_a_paused_run_is_graded_as_an_error_condition(self):
        # An answer nobody approved is not an answer. `no_error` has to catch
        # it, or an empty string gets graded as a bad reply.
        self.case(graders=[{'type': 'no_error'}])
        run = self.sweep(stub(agent_run(answer='', awaiting_approval=True)))

        result = run.results.get()
        self.assertFalse(result.auto_passed)
        self.assertIn('approval', result.grades[0]['detail'])

    def test_one_broken_case_does_not_stop_the_sweep(self):
        self.case()
        self.case(goal='second')
        calls = []

        async def flaky(agent, goal, **kwargs):
            calls.append(goal)
            if len(calls) == 1:
                raise RuntimeError('provider hiccup')
            return agent_run('Paris')

        run = self.sweep(flaky)
        self.assertEqual(run.total_cases, 2)
        self.assertEqual(run.error_count, 1)
        self.assertEqual(run.passed_count, 1)
        # The outage drags the score down rather than vanishing from it.
        self.assertAlmostEqual(run.score, 0.5)

    def test_a_guardrail_refusal_stops_the_whole_sweep(self):
        for i in range(4):
            self.case(goal=f'case {i}')
        run = self.sweep(stub(raises=AgentRunRefused('spend cap reached')))

        self.assertEqual(run.status, 'failed')
        self.assertIn('spend cap', run.error_message)
        # The remaining cases are skipped, not filled with the same message
        # four times over.
        self.assertLess(
            EvalResult.objects.filter(run=run, status='error').count(), 4,
        )
        self.assertTrue(EvalResult.objects.filter(run=run, status='skipped').exists())

    def test_a_cancelled_run_stops_at_the_next_case(self):
        for i in range(4):
            self.case(goal=f'case {i}')
        run = async_to_sync(runner.open_run)(self.suite, self.agent, self.user, '')

        cancel = sync_to_async(
            lambda: EvalRun.objects.filter(pk=run.pk).update(status='cancelled')
        )

        async def cancel_then_answer(agent, goal, **kwargs):
            await cancel()
            return agent_run()

        with patch('agents.agent.runtime.run_agent', cancel_then_answer):
            async_to_sync(runner.sweep)(run, self.suite, self.agent, self.user)

        run.refresh_from_db()
        self.assertEqual(run.status, 'cancelled')
        self.assertTrue(EvalResult.objects.filter(run=run, status='skipped').exists())

    def test_an_empty_suite_is_refused_rather_than_scored(self):
        with self.assertRaises(runner.NoCasesToRun):
            async_to_sync(runner.start_suite_run)(self.suite, self.agent, self.user)
        self.assertEqual(EvalRun.objects.count(), 0)

    def test_the_answer_is_capped_and_says_so(self):
        from workflow_backend.thresholds import EVAL_RESULT_ANSWER_CHAR_LIMIT

        self.case(graders=[])
        long_answer = 'paris ' * EVAL_RESULT_ANSWER_CHAR_LIMIT
        run = self.sweep(stub(agent_run(long_answer)))

        result = run.results.get()
        self.assertEqual(len(result.answer), EVAL_RESULT_ANSWER_CHAR_LIMIT)
        self.assertTrue(result.answer_truncated)
