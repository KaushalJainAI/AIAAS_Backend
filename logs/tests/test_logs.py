"""
Coverage for every route in `logs/urls.py`.

This file previously collected *zero* tests - one was commented out at its
`def` line and the other renamed `_test_...` - which is why four of the app's
endpoints could 500 on a stale `workflow` FK without anyone noticing. Every
route now has at least one test that actually exercises the ORM, because the
failure mode being guarded against is a query that no longer matches the schema.
"""
import uuid
from unittest import mock

from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase

from agents.models import SubAgent
from logs import queries
from logs.models import AgentStep, AgentTurn, ExecutionLog


class LogsTestBase(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='analyst', password='pw')
        self.other = User.objects.create_user(username='stranger', password='pw')
        self.client.force_authenticate(user=self.user)

        self.agent = SubAgent.objects.create(user=self.user, name='Test Agent')
        self.execution = ExecutionLog.objects.create(
            user=self.user,
            subagent=self.agent,
            status='completed',
            trigger_type='manual',
            duration_ms=1500,
            nodes_executed=2,
            tokens_used=300,
            credits_used=3,
            started_at=timezone.now(),
        )
        self.failed = ExecutionLog.objects.create(
            user=self.user, subagent=self.agent, status='failed', duration_ms=500,
            tokens_used=100,
        )
        self.turn = AgentTurn.objects.create(
            execution=self.execution, index=1, decision='tools',
            reasoning='I will search, then read the best hit.',
            provider='openrouter', model_id='m', tokens=120, duration_ms=90,
        )
        AgentStep.objects.create(
            execution=self.execution, turn=self.turn, call_id='c1',
            tool='web_search', status='completed', order=1, duration_ms=200,
        )
        AgentStep.objects.create(
            execution=self.execution, turn=self.turn, call_id='c2',
            tool='read_url', status='failed', order=2, duration_ms=100,
        )


class ExecutionStatisticsTests(LogsTestBase):
    url = None

    def setUp(self):
        super().setUp()
        self.url = reverse('logs:execution_statistics')

    def test_summary_counts_runs_by_status(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        summary = response.data['summary']
        self.assertEqual(summary['total_executions'], 2)
        self.assertEqual(summary['successful'], 1)
        self.assertEqual(summary['failed'], 1)
        self.assertEqual(summary['success_rate'], 50.0)
        self.assertEqual(summary['total_tokens_used'], 400)
        self.assertEqual(response.data['by_status'], {'completed': 1, 'failed': 1})
        self.assertEqual(response.data['by_trigger']['manual'], 2)

    def test_daily_trend_dates_are_iso_strings(self):
        trend = self.client.get(self.url).data['daily_trend']
        self.assertEqual(len(trend), 1)
        self.assertIsInstance(trend[0]['date'], str)
        self.assertEqual(trend[0]['success'], 1)

    def test_workflow_id_filters_by_agent(self):
        """Regression: this filter hit the removed `workflow` FK and 500'd."""
        response = self.client.get(self.url, {'workflow_id': self.agent.id})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['summary']['total_executions'], 2)

        other = SubAgent.objects.create(user=self.user, name='Empty')
        response = self.client.get(self.url, {'workflow_id': other.id})
        self.assertEqual(response.data['summary']['total_executions'], 0)
        self.assertEqual(response.data['summary']['success_rate'], 0)

    def test_rejects_out_of_range_days(self):
        for bad in ('thirty', 0, 400):
            with self.subTest(days=bad):
                response = self.client.get(self.url, {'days': bad})
                self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
                self.assertIn('days', response.data)

    def test_excludes_other_users(self):
        ExecutionLog.objects.create(user=self.other, status='completed')
        self.assertEqual(self.client.get(self.url).data['summary']['total_executions'], 2)

    def test_requires_authentication(self):
        self.client.force_authenticate(user=None)
        self.assertIn(
            self.client.get(self.url).status_code,
            (status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN),
        )


class WorkflowMetricsTests(LogsTestBase):
    def test_reports_per_tool_success_rates(self):
        url = reverse('logs:workflow_metrics', args=[self.agent.id])
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['workflow_name'], 'Test Agent')
        self.assertEqual(response.data['total_executions'], 2)
        self.assertEqual(response.data['success_rate'], 50.0)
        # Keyed by tool, which is the unit an agent actually has: "read_url
        # always fails" is actionable in a way a call id never was.
        rates = response.data['tool_success_rates']
        self.assertEqual(rates['web_search']['success_rate'], 100.0)
        self.assertEqual(rates['read_url']['success_rate'], 0)
        self.assertEqual(response.data['error_hotspots'][0]['tool'], 'read_url')

    def test_recent_executions_use_the_workflow_wire_names(self):
        url = reverse('logs:workflow_metrics', args=[self.agent.id])
        recent = self.client.get(url).data['recent_executions'][0]
        self.assertEqual(recent['workflow_id'], self.agent.id)
        self.assertEqual(recent['workflow_name'], 'Test Agent')
        self.assertNotIn('subagent_id', recent)

    def test_other_users_agent_is_not_found(self):
        theirs = SubAgent.objects.create(user=self.other, name='Theirs')
        url = reverse('logs:workflow_metrics', args=[theirs.id])
        self.assertEqual(self.client.get(url).status_code, status.HTTP_404_NOT_FOUND)


class CostBreakdownTests(LogsTestBase):
    def test_totals_and_breakdowns(self):
        response = self.client.get(reverse('logs:cost_breakdown'))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['total_tokens'], 400)
        self.assertEqual(response.data['total_credits'], 3)
        row = response.data['by_workflow'][0]
        self.assertEqual(row['workflow_id'], self.agent.id)
        self.assertEqual(row['workflow_name'], 'Test Agent')
        self.assertNotIn('subagent__id', row)
        tools = {row['tool'] for row in response.data['by_tool']}
        self.assertEqual(tools, {'web_search', 'read_url'})
        self.assertIsInstance(response.data['daily_usage'][0]['date'], str)


class ExecutionListTests(LogsTestBase):
    def setUp(self):
        super().setUp()
        self.url = reverse('logs:execution_list')

    def test_lists_runs_with_agent_name(self):
        """Regression: `select_related('workflow')` 500'd on every call."""
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['count'], 2)
        row = response.data['results'][0]
        self.assertEqual(row['workflow_id'], self.agent.id)
        self.assertEqual(row['workflow_name'], 'Test Agent')

    def test_filters_by_status_and_agent(self):
        response = self.client.get(self.url, {'status': 'failed'})
        self.assertEqual(response.data['count'], 1)
        self.assertEqual(response.data['results'][0]['status'], 'failed')

        response = self.client.get(self.url, {'workflow_id': self.agent.id})
        self.assertEqual(response.data['count'], 2)

    def test_pages_by_cursor_without_repeating_rows(self):
        first = self.client.get(self.url, {'limit': 1})
        self.assertTrue(first.data['has_more'])
        self.assertEqual(first.data['count'], 2)

        second = self.client.get(self.url, {'limit': 1, 'cursor': first.data['next_cursor']})
        self.assertFalse(second.data['has_more'])
        # A cursored page skips the count query, so it reports None rather than
        # paying for a total the caller already has.
        self.assertIsNone(second.data['count'])
        self.assertNotEqual(
            first.data['results'][0]['execution_id'],
            second.data['results'][0]['execution_id'],
        )

    def test_rejects_bad_cursor_and_limit(self):
        self.assertEqual(
            self.client.get(self.url, {'cursor': 'not-base64'}).status_code,
            status.HTTP_400_BAD_REQUEST,
        )
        self.assertEqual(
            self.client.get(self.url, {'limit': 500}).status_code,
            status.HTTP_400_BAD_REQUEST,
        )

    def test_excludes_other_users(self):
        ExecutionLog.objects.create(user=self.other, status='completed')
        self.assertEqual(self.client.get(self.url).data['count'], 2)


class ExecutionDetailTests(LogsTestBase):
    def _url(self, execution_id):
        return reverse('logs:execution_detail', args=[str(execution_id)])

    def test_returns_the_run_as_turns_holding_their_own_steps(self):
        response = self.client.get(self._url(self.execution.execution_id))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['workflow_id'], self.agent.id)
        self.assertEqual(response.data['workflow_name'], 'Test Agent')
        self.assertEqual(response.data['credits_used'], 3)

        turns = response.data['turns']
        self.assertEqual(len(turns), 1)
        self.assertEqual(turns[0]['index'], 1)
        self.assertEqual(turns[0]['model_id'], 'm')
        # The reasoning is the whole point: it used to be a 150-character
        # slice copied onto each step, and discarded when the run closed.
        self.assertEqual(turns[0]['reasoning'],
                         'I will search, then read the best hit.')
        self.assertFalse(turns[0]['reasoning_truncated'])

        tools = [step['tool'] for step in turns[0]['steps']]
        self.assertEqual(tools, ['web_search', 'read_url'])

    def test_a_step_with_no_turn_is_surfaced_not_dropped(self):
        """A backfilled run, or a turn whose write failed. The agent still did
        the work, so the step still has to appear somewhere."""
        AgentStep.objects.create(
            execution=self.execution, turn=None, call_id='orphan',
            tool='execute_python', status='completed', order=9,
        )
        response = self.client.get(self._url(self.execution.execution_id))
        orphans = response.data['unattributed_steps']
        self.assertEqual([s['call_id'] for s in orphans], ['orphan'])

    def test_reports_whether_steps_were_truncated(self):
        response = self.client.get(self._url(self.execution.execution_id))
        self.assertEqual(response.data['step_total'], 2)
        self.assertFalse(response.data['steps_truncated'])

    def test_caps_steps_at_the_configured_limit(self):
        AgentStep.objects.bulk_create([
            AgentStep(
                execution=self.execution, turn=self.turn, call_id=f'x{i}',
                tool='t', status='completed', order=10 + i,
            )
            for i in range(5)
        ])
        # `queries` imports the constant by value, so it is patched there.
        with mock.patch.object(queries, 'EXECUTION_NODE_LOG_LIMIT', 3):
            response = self.client.get(self._url(self.execution.execution_id))

        drawn = sum(len(turn['steps']) for turn in response.data['turns'])
        self.assertEqual(drawn, 3)
        self.assertEqual(response.data['step_total'], 7)
        self.assertTrue(response.data['steps_truncated'])

    def test_caps_turns_independently_of_steps(self):
        """A turn's reasoning is stored in full, so the steps cap alone would
        not bound the response — this is what bounds it."""
        for i in range(2, 6):
            AgentTurn.objects.create(
                execution=self.execution, index=i, decision='tools',
                reasoning='r' * 100, tokens=10,
            )
        with mock.patch.object(queries, 'EXECUTION_TURN_LIMIT', 2):
            response = self.client.get(self._url(self.execution.execution_id))

        self.assertEqual(response.data['turn_total'], 5)
        self.assertEqual([t['index'] for t in response.data['turns']], [1, 2])
        self.assertTrue(response.data['turns_truncated'])

    def test_unknown_and_malformed_ids_are_404(self):
        self.assertEqual(
            self.client.get(self._url(uuid.uuid4())).status_code,
            status.HTTP_404_NOT_FOUND,
        )
        # The URL captures a free `str`, so a non-UUID reaches the query layer.
        self.assertEqual(
            self.client.get(self._url('not-a-uuid')).status_code,
            status.HTTP_404_NOT_FOUND,
        )

    def test_another_user_cannot_read_the_run(self):
        self.client.force_authenticate(user=self.other)
        self.assertEqual(
            self.client.get(self._url(self.execution.execution_id)).status_code,
            status.HTTP_404_NOT_FOUND,
        )


class RetiredRouteTests(LogsTestBase):
    """The DAG-era read surface is gone, not merely empty."""

    def test_audit_and_narrative_routes_no_longer_resolve(self):
        for path in (
            '/api/logs/audit/',
            '/api/logs/audit/export/',
            f'/api/logs/executions/{self.execution.execution_id}/activities/',
            f'/api/logs/executions/{self.execution.execution_id}/narrative/',
        ):
            with self.subTest(path=path):
                self.assertEqual(
                    self.client.get(path).status_code, status.HTTP_404_NOT_FOUND
                )
