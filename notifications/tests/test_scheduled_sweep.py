"""
The scheduled sweep: user-asked reminders fired at their time, on their beat.

Pinned: a one-shot goes quiet after firing; a repeat advances from the due
time (a late sweep must not shift the heartbeat); delivery is the same shape
as `notify_user` (feed row always, device ping unless quiet hours, web-push
twin, never email); and one bad row logs and moves on instead of killing the
sweep.
"""
from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from notifications.models import Notification, ScheduledNotification
from notifications.scheduled import run_scheduled_sweep

User = get_user_model()


class ScheduledSweepTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user('owner', 'o@example.com', 'pw')

    def _reminder(self, **overrides):
        body = {
            'user': self.user, 'title': 'Standup', 'message': 'Send notes.',
            'next_run_at': timezone.now() - timedelta(minutes=1),
        }
        body.update(overrides)
        return ScheduledNotification.objects.create(**body)

    def test_one_shot_fires_then_goes_quiet(self):
        row = self._reminder()
        result = run_scheduled_sweep()
        self.assertEqual(result, {'sent': 1})
        row.refresh_from_db()
        self.assertFalse(row.active)
        self.assertIsNone(row.next_run_at)
        self.assertEqual(row.times_sent, 1)
        notif = Notification.objects.get(user=self.user)
        self.assertEqual(notif.type, 'scheduled_reminder')
        self.assertEqual(notif.title, 'Standup')
        # Second pass finds nothing to do.
        self.assertEqual(run_scheduled_sweep(), {'sent': 0})

    def test_repeat_advances_from_the_due_time_not_from_now(self):
        due = timezone.now() - timedelta(hours=2)
        row = self._reminder(repeat='hourly', next_run_at=due)
        run_scheduled_sweep()
        row.refresh_from_db()
        self.assertTrue(row.active)
        self.assertEqual(row.next_run_at, due + timedelta(hours=1))

    def test_future_and_cancelled_rows_are_untouched(self):
        self._reminder(next_run_at=timezone.now() + timedelta(hours=1))
        quiet = self._reminder()
        quiet.cancel()
        self.assertEqual(run_scheduled_sweep(), {'sent': 0})
        self.assertEqual(Notification.objects.count(), 0)

    def test_quiet_hours_keep_the_row_but_mute_the_ping(self):
        from notifications.models import NotificationPreference

        now = timezone.now()
        NotificationPreference.objects.update_or_create(
            user=self.user,
            defaults={
                'device_notifications_enabled': True,
                'quiet_hours_enabled': True,
                'quiet_hours_start': (now - timedelta(hours=1)).time(),
                'quiet_hours_end': (now + timedelta(hours=1)).time(),
            })
        self._reminder()
        with patch('notifications.webpush.send_web_push') as push:
            run_scheduled_sweep()
        push.assert_not_called()
        self.assertEqual(Notification.objects.count(), 1)

    def test_device_ping_fires_outside_quiet_hours(self):
        self._reminder()
        with patch('notifications.webpush.send_web_push') as push:
            run_scheduled_sweep()
        push.assert_called_once()

    def test_email_opt_in_rides_along_and_defaults_off(self):
        from notifications.scheduled import _deliver

        plain = self._reminder()
        with patch('notifications.utils.create_notification') as create:
            create.return_value = object()
            _deliver(plain, timezone.now())
        self.assertFalse(create.call_args.kwargs['send_email'])

        emailed = self._reminder(send_email=True)
        with patch('notifications.utils.create_notification') as create:
            create.return_value = object()
            _deliver(emailed, timezone.now())
        self.assertTrue(create.call_args.kwargs['send_email'])

    def test_one_bad_row_does_not_kill_the_sweep(self):
        self._reminder(title='First')
        self._reminder(title='Second')
        with patch('notifications.utils.create_notification',
                   side_effect=[Exception('down'), object()]):
            result = run_scheduled_sweep()
        # The failure logged and the sweep moved on: one sent, one still live.
        self.assertEqual(result, {'sent': 1})
        self.assertEqual(
            ScheduledNotification.objects.filter(active=True).count(), 1)
