"""Explicit feedback: upsert, delete, 404 on foreign targets, one-target constraint."""
from django.contrib.auth.models import User
from django.db import IntegrityError
from django.test import TestCase
from rest_framework.test import APIClient

from agents.models import SubAgent
from logs.models import ExecutionLog, Feedback


class FeedbackTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user('f', 'f@example.com', 'pw')
        self.other = User.objects.create_user('o', 'o@example.com', 'pw')
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)
        self.agent = SubAgent.objects.create(user=self.user, name='A')
        self.log = ExecutionLog.objects.create(
            subagent=self.agent, user=self.user, status='completed')

    def test_upsert_then_delete(self):
        r = self.client.put('/api/logs/feedback/', {
            'target': 'execution', 'id': str(self.log.execution_id),
            'rating': -1, 'reason': 'wrong', 'comment': 'bad answer',
        }, format='json')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data['feedback']['rating'], -1)
        # Re-rating updates rather than doubling.
        r2 = self.client.put('/api/logs/feedback/', {
            'target': 'execution', 'id': str(self.log.execution_id), 'rating': 1,
        }, format='json')
        self.assertEqual(r2.status_code, 200)
        self.assertEqual(Feedback.objects.filter(user=self.user).count(), 1)
        d = self.client.delete(
            f"/api/logs/feedback/?target=execution&id={self.log.execution_id}")
        self.assertEqual(d.status_code, 200)
        self.assertEqual(Feedback.objects.filter(user=self.user).count(), 0)

    def test_foreign_target_404s(self):
        other_log = ExecutionLog.objects.create(
            subagent=self.agent, user=self.other, status='completed')
        r = self.client.put('/api/logs/feedback/', {
            'target': 'execution', 'id': str(other_log.execution_id), 'rating': 1,
        }, format='json')
        self.assertEqual(r.status_code, 404)

    def test_both_targets_refused(self):
        from chat.models import ChatMessage, ChatSession

        session = ChatSession.objects.create(user=self.user, title='t')
        msg = ChatMessage.objects.create(session=session, role='assistant', content='hi')
        with self.assertRaises(IntegrityError):
            Feedback.objects.create(user=self.user, execution=self.log,
                                    chat_message=msg, rating=1)
