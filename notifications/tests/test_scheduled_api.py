"""
The reminders management surface: list and cancel, owner-only.

Pinned: creation is not here (chat quotes the user's timing — a second write
path is a second place to forget that rule); a foreign id is 404, never 403;
cancelling something already spent is a quiet 404, not an error about state
the caller never saw.
"""
from __future__ import annotations

from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from notifications.models import ScheduledNotification

User = get_user_model()


class ScheduledApiTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user('owner', 'o@example.com', 'pw')
        self.other = User.objects.create_user('other', 't@example.com', 'pw')
        self.client = APIClient()
        self.client.force_authenticate(self.user)
        self.row = ScheduledNotification.objects.create(
            user=self.user, title='Standup', message='Send notes.',
            repeat='daily', send_email=True,
            next_run_at=timezone.now() + timedelta(hours=1))

    def test_list_shows_only_live_own_rows(self):
        res = self.client.get('/api/notifications/scheduled/')
        self.assertEqual(res.status_code, 200)
        rows = res.data['results'] if isinstance(res.data, dict) else res.data
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['repeat'], 'daily')
        self.assertTrue(rows[0]['send_email'])
        other = APIClient()
        other.force_authenticate(self.other)
        other_res = other.get('/api/notifications/scheduled/')
        other_rows = (other_res.data['results']
                      if isinstance(other_res.data, dict) else other_res.data)
        self.assertEqual(other_rows, [])

    def test_cancel_is_idempotent_and_owner_only(self):
        url = f'/api/notifications/scheduled/{self.row.id}/'
        other = APIClient()
        other.force_authenticate(self.other)
        self.assertEqual(other.delete(url).status_code, 404)
        self.assertEqual(self.client.delete(url).status_code, 204)
        self.row.refresh_from_db()
        self.assertFalse(self.row.active)
        self.assertIsNone(self.row.next_run_at)
        # Already spent: quiet 404, not a state error.
        self.assertEqual(self.client.delete(url).status_code, 404)

    def test_unauthenticated_is_refused(self):
        anon = APIClient()
        res = anon.get('/api/notifications/scheduled/')
        self.assertIn(res.status_code, (401, 403))
