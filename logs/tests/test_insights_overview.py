"""Insights overview improvements (2026-09-21 plan): sorted spend, examples,
spend-by-kind, payer honesty, previous-window comparison, failure filter."""
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone

from agents.models import HITLRequest, SubAgent
from logs.costs import record
from logs.models import AgentStep, ExecutionLog


class InsightsOverviewTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user('ins', 'ins@example.com', 'pw')
        self.cheap = SubAgent.objects.create(user=self.user, name='Cheap')
        self.dear = SubAgent.objects.create(user=self.user, name='Dear')

    def _run(self, agent, **kwargs):
        defaults = dict(
            user=self.user, subagent=agent, status='completed',
            tokens_used=1000, cost_usd=Decimal('0.010000'), cost_source='billed',
        )
        defaults.update(kwargs)
        return ExecutionLog.objects.create(**defaults)

    def test_most_expensive_sorts_by_money_not_tokens(self):
        # Cheap has 10x the tokens but a tenth of the spend.
        self._run(self.cheap, tokens_used=100_000, cost_usd=Decimal('0.010000'))
        self._run(self.dear, tokens_used=1_000, cost_usd=Decimal('0.500000'))
        from logs.queries import insights_overview

        data = insights_overview(self.user, days=30)
        self.assertEqual(data['agents']['most_expensive'][0]['workflow_name'], 'Dear')

    def test_unpriced_sorts_last_never_as_free(self):
        self._run(self.cheap, tokens_used=100_000, cost_source='unpriced',
                  cost_usd=Decimal('0'))
        self._run(self.dear, tokens_used=10, cost_usd=Decimal('0.010000'))
        from logs.queries import insights_overview

        rows = insights_overview(self.user, days=30)['agents']['most_expensive']
        self.assertEqual(rows[-1]['workflow_name'], 'Cheap')
        self.assertEqual(rows[-1]['cost_source'], 'unpriced')

    def test_tool_and_agent_examples_link_to_failing_runs(self):
        run = self._run(self.cheap, status='failed', failure_category='tool_error',
                        error_message='boom')
        AgentStep.objects.create(
            execution=run, tool='web_search', status='failed',
            call_id='c1', order=0, error_message='dns blew up',
        )
        from logs.queries import insights_overview

        data = insights_overview(self.user, days=30)
        tool = next(t for t in data['tools'] if t['tool'] == 'web_search')
        self.assertEqual(tool['execution_id'], str(run.execution_id))
        self.assertIn('dns', tool['error'])
        leader = next(r for r in data['agents']['most_active']
                      if r['workflow_name'] == 'Cheap')
        self.assertEqual(leader['example_execution_id'], str(run.execution_id))

    def test_median_approve_ms_present(self):
        run = self._run(self.cheap)
        req = HITLRequest.objects.create(
            user=self.user, execution=run, request_type='approval',
            title='T', message='M', status='approved',
        )
        # Answer 60s after open.
        req.responded_at = req.created_at + timezone.timedelta(seconds=60)
        req.save(update_fields=['responded_at'])
        from logs.queries import insights_overview

        data = insights_overview(self.user, days=30)
        self.assertEqual(data['agents']['median_approve_ms'], 60_000)

    def test_spend_by_kind_and_provenance_split(self):
        run = self._run(self.cheap)
        record(user=self.user, kind='sms', amount_inr=7, execution=run,
               units=3, unit='messages', estimated=True, source='message_send:c1')
        from logs.queries import cost_breakdown

        data = cost_breakdown(self.user, days=30)
        self.assertTrue(any(r['kind'] == 'sms' and r['amount_inr'] == 7
                            for r in data['by_kind']))
        self.assertEqual(data['agents_by_cost_source']['billed'], 1)

    def test_previous_window_returned_on_compare(self):
        self._run(self.cheap)
        from logs.queries import insights_overview

        data = insights_overview(self.user, days=30, compare=True)
        self.assertIn('previous', data)
        self.assertEqual(data['previous']['total_executions'], 0)
        plain = insights_overview(self.user, days=30)
        self.assertNotIn('previous', plain)


class ExecutionFailureFilterTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user('ff', 'ff@example.com', 'pw')
        self.agent = SubAgent.objects.create(user=self.user, name='A')

    def test_failure_category_filters(self):
        from logs.queries import execution_page

        ExecutionLog.objects.create(
            user=self.user, subagent=self.agent, status='failed',
            failure_category='provider')
        ExecutionLog.objects.create(
            user=self.user, subagent=self.agent, status='failed',
            failure_category='tool_error')
        page = execution_page(self.user, limit=20, failure_category='provider')
        self.assertEqual(page['count'], 1)
        self.assertEqual(page['results'][0]['failure_category'], 'provider')
