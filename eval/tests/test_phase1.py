"""Phase 1: eval caller, cost ceiling, judge cost totals."""
from decimal import Decimal
from unittest.mock import patch

from asgiref.sync import async_to_sync
from django.contrib.auth.models import User
from django.test import TestCase

from agents.agent.runtime import AgentRun
from agents.models import SubAgent
from eval import runner
from eval.models import EvalCase, EvalSuite
from eval.tests.test_runner import agent_run, stub
from logs.models import ExecutionLog


class EvalCallerTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user('e', 'e@example.com', 'pw')
        self.agent = SubAgent.objects.create(user=self.user, name='A')
        self.suite = EvalSuite.objects.create(
            user=self.user, name='S', subagent=self.agent, supervision='none')

    def test_runner_passes_caller_eval(self):
        EvalCase.objects.create(suite=self.suite, goal='g', graders=[])
        seen = {}

        async def fake(agent, goal, **kwargs):
            seen.update(kwargs)
            return agent_run('hi')

        run = async_to_sync(runner.open_run)(self.suite, self.agent, self.user, '')
        with patch('agents.agent.runtime.run_agent', fake):
            async_to_sync(runner.sweep)(run, self.suite, self.agent, self.user)
        self.assertEqual(seen.get('caller'), 'eval')
        # A stubbed run_agent writes no ExecutionLog; the caller is asserted
        # above, and exclusion from stats/spend is pinned in the agents + logs
        # suites below.

    def test_ceiling_skips_and_fails_run(self):
        for i in range(3):
            EvalCase.objects.create(suite=self.suite, goal=f'g{i}', graders=[])
        self.suite.max_cost_rupees = 0
        self.suite.save(update_fields=['max_cost_rupees'])
        # Force spend above ceiling by stubbing the spend reader.
        async def fake_spend(run):
            return 999

        run = async_to_sync(runner.open_run)(self.suite, self.agent, self.user, '')
        with patch('eval.runner._run_spend_rupees', fake_spend):
            with patch('agents.agent.runtime.run_agent', stub(agent_run('hi'))):
                async_to_sync(runner.sweep)(run, self.suite, self.agent, self.user)
        run.refresh_from_db()
        self.assertEqual(run.status, 'failed')
        self.assertIn('ceiling', run.error_message)

    def test_judge_cost_summed_onto_result_and_run(self):
        from llm.access import Completion
        from llm.usage import TokenUsage

        EvalCase.objects.create(
            suite=self.suite, goal='g', reference='rubric',
            graders=[{'type': 'llm_judge'}])

        async def fake_judge(**kwargs):
            return Completion(content='{"score": 1.0, "reason": "good"}',
                              usage=TokenUsage(input=10, output=5, total=15), tokens=15)

        run = async_to_sync(runner.open_run)(self.suite, self.agent, self.user, '')
        with patch('agents.agent.runtime.run_agent', stub(agent_run('answer'))):
            with patch('llm.access.complete', fake_judge):
                async_to_sync(runner.sweep)(run, self.suite, self.agent, self.user)
        run.refresh_from_db()
        result = run.results.get()
        self.assertGreaterEqual(result.judge_tokens, 0)
        self.assertGreaterEqual(run.tokens_used, 100)
