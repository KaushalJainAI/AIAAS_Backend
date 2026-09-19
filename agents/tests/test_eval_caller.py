"""Eval runs are invisible to stats, spend caps, lists — except cost breakdown."""
from asgiref.sync import async_to_sync
from django.contrib.auth.models import User
from django.test import TestCase
from rest_framework.test import APITestCase

from agents.agent.runtime import check_guardrails
from agents.models import SubAgent
from logs.models import ExecutionLog


class EvalExclusionTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user('u', 'u@example.com', 'pw')
        self.client.force_authenticate(user=self.user)
        self.agent = SubAgent.objects.create(user=self.user, name='A')

    def _log(self, caller='api', tokens=1000):
        return ExecutionLog.objects.create(
            subagent=self.agent, user=self.user, status='completed',
            caller=caller, tokens_used=tokens)

    def test_eval_not_counted_in_runs_or_spend(self):
        self._log('api', tokens=2_000_000)
        self._log('eval', tokens=2_000_000)
        stats = self.client.get('/api/orchestrator/agents/').data[0]
        self.assertEqual(stats['runs'], 1)

    def test_eval_spend_does_not_trip_cap(self):
        self._log('eval', tokens=9_000_000)
        self.agent.guardrails = {'spendCapRupees': 10}
        self.agent.save(update_fields=['guardrails'])
        # Must not raise: eval spend is excluded.
        async_to_sync(check_guardrails)(self.agent, self.user)


class EvalRecoveryTests(TestCase):
    def test_orphaned_eval_is_failed_not_resumed(self):
        from django.utils import timezone

        user = User.objects.create_user('r', 'r@example.com', 'pw')
        agent = SubAgent.objects.create(user=user, name='A')
        log = ExecutionLog.objects.create(
            subagent=agent, user=user, status='running', caller='eval',
            started_at=timezone.now() - timezone.timedelta(seconds=99999),
            input_data={'goal': 'g', 'thread_id': 't-eval'})
        from asgiref.sync import async_to_sync

        from agents import recovery

        tally = async_to_sync(recovery.sweep_orphaned_runs)(limit=5)
        log.refresh_from_db()
        self.assertEqual(log.status, 'failed')
        self.assertGreaterEqual(tally['failed'], 1)
