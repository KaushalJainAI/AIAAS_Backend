"""Each signal writer records exactly one row; a failing write never fails the action."""
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase

from agents.models import SubAgent
from logs import signals_api
from logs.models import ExecutionLog, RunSignal


class SignalTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user('s', 's@example.com', 'pw')
        self.agent = SubAgent.objects.create(user=self.user, name='A')
        self.log = ExecutionLog.objects.create(
            subagent=self.agent, user=self.user, status='completed')

    def test_record_once(self):
        signals_api.record_signal(self.user.id, 'cancelled',
                                  execution_id=str(self.log.execution_id))
        self.assertEqual(RunSignal.objects.filter(user=self.user, kind='cancelled').count(), 1)

    def test_failing_write_does_not_raise(self):
        with patch('logs.models.RunSignal.objects.create', side_effect=RuntimeError('db down')):
            signals_api.record_signal(self.user.id, 'steered', session_id='s1')
        self.assertEqual(RunSignal.objects.count(), 0)
