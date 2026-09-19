"""execution_page hides eval by default; cost_breakdown reports it as its own line."""
from django.contrib.auth.models import User
from django.test import TestCase

from agents.models import SubAgent
from logs import queries
from logs.models import ExecutionLog


class EvalExclusionQueriesTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user('q', 'q@example.com', 'pw')
        self.agent = SubAgent.objects.create(user=self.user, name='A')
        ExecutionLog.objects.create(subagent=self.agent, user=self.user,
                                    status='completed', caller='api')
        ExecutionLog.objects.create(subagent=self.agent, user=self.user,
                                    status='completed', caller='eval')

    def test_execution_page_hides_eval_by_default(self):
        page = queries.execution_page(self.user, limit=20)
        self.assertEqual(len(page['results']), 1)
        self.assertEqual(page['results'][0]['caller'], 'api')

    def test_execution_page_shows_eval_on_request(self):
        page = queries.execution_page(self.user, limit=20, caller='eval')
        self.assertEqual(len(page['results']), 1)
        self.assertEqual(page['results'][0]['caller'], 'eval')

    def test_cost_breakdown_has_eval_line(self):
        breakdown = queries.cost_breakdown(self.user, days=30)
        self.assertIn('by_caller', breakdown)
        self.assertEqual(breakdown['by_caller'].get('eval'), 1)

    def test_statistics_exclude_eval(self):
        stats = queries.execution_statistics(self.user, days=30)
        self.assertEqual(stats['summary']['total_executions'], 1)
