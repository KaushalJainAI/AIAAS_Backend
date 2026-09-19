"""A bad run becomes a test."""
from django.contrib.auth.models import User
from django.test import TestCase
from rest_framework.test import APIClient

from agents.models import SubAgent
from eval.models import EvalCase, EvalSuite
from logs.models import ExecutionLog


class CaseFromRunTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user('c', 'c@example.com', 'pw')
        self.other = User.objects.create_user('o', 'o@example.com', 'pw')
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)
        self.agent = SubAgent.objects.create(user=self.user, name='A')

    def _log(self, **kwargs):
        defaults = dict(subagent=self.agent, user=self.user, status='completed',
                        input_data={'goal': 'do it', 'thread_id': 't-1', '_secret': 1},
                        output_data={'tool_trace': [{'tool': 'read_file', 'args': {'path': '/a.md'}}]})
        return ExecutionLog.objects.create(**{**defaults, **kwargs})

    def test_creates_from_runs_suite_once(self):
        log = self._log()
        r1 = self.client.post('/api/eval/cases/from-run/', {'execution_id': str(log.execution_id)}, format='json')
        self.assertEqual(r1.status_code, 201)
        r2 = self.client.post('/api/eval/cases/from-run/', {'execution_id': str(log.execution_id)}, format='json')
        self.assertEqual(r2.status_code, 201)
        self.assertEqual(EvalSuite.objects.filter(user=self.user, name='From runs').count(), 1)
        case = EvalCase.objects.get(pk=r1.data['id'])
        self.assertEqual(case.graders, [])
        self.assertIn('from-run', case.tags)
        self.assertIn(str(log.execution_id), case.tags)
        # Runtime keys stripped; goal lifted out.
        self.assertNotIn('thread_id', case.input_data)
        self.assertNotIn('_secret', case.input_data)
        self.assertEqual(case.goal, 'do it')

    def test_ownership_404s(self):
        log = self._log()
        other_client = APIClient()
        other_client.force_authenticate(user=self.other)
        r = other_client.post('/api/eval/cases/from-run/', {'execution_id': str(log.execution_id)}, format='json')
        self.assertEqual(r.status_code, 404)
