"""Each terminal path classifies correctly, including recovery."""
from django.contrib.auth.models import User
from django.test import TestCase

from agents.models import SubAgent
from logs import failures
from logs.models import ExecutionLog


class FailureCategoryTests(TestCase):
    def test_types_first(self):
        self.assertEqual(failures.classify('failed', '', exc=RuntimeError('GraphRecursionError boom')), 'step_budget')
        self.assertEqual(failures.classify('failed', 'spend cap reached',
                                           exc=type('AgentRunRefused', (RuntimeError,), {})('x')), 'guardrail')

    def test_recovery_is_interrupted(self):
        user = User.objects.create_user('u', 'u@example.com', 'pw')
        agent = SubAgent.objects.create(user=user, name='A')
        log = ExecutionLog.objects.create(subagent=agent, user=user, status='running')
        self.assertEqual(failures.classify('failed', 'interrupted — the server restarted'), 'interrupted')
        self.assertTrue(hasattr(log, 'failure_category'))
