"""
HITL reminder engine tests.

The contract being protected here is the delivery split the feature exists for:
escalation and hourly nudges are device-only, and email happens once a day at
most. A regression that starts mailing every escalation is the failure mode
worth a test.
"""

from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from logs.models import ExecutionLog
from agents.models import HITLRequest, SubAgent

from notifications.models import HITLReminderSchedule, Notification, NotificationPreference
from notifications.reminders import get_preferences, run_reminder_sweep


class ReminderTestCase(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username='hitl-user', email='hitl@example.com', password='password123'
        )
        self.workflow = SubAgent.objects.create(user=self.user, name='wf')
        self.execution = ExecutionLog.objects.create(
            user=self.user, subagent=self.workflow, status='running'
        )

    def make_request(self, title='Approve the spend', **kwargs):
        # The immediate nudge is armed in transaction.on_commit, which never
        # fires under TestCase's wrapping transaction.
        with self.captureOnCommitCallbacks(execute=True):
            request = HITLRequest.objects.create(
                execution=self.execution,
                user=self.user,
                request_type='approval',
                title=title,
                message='The agent needs a decision to continue.',
                **kwargs,
            )
        return request


class EscalationLadderTests(ReminderTestCase):
    @patch('notifications.reminders.push_device_notification')
    def test_creating_a_request_notifies_immediately_and_arms_the_ladder(self, push):
        request = self.make_request()

        schedule = HITLReminderSchedule.objects.get(hitl_request=request)
        self.assertEqual(schedule.reminders_sent, 1)
        self.assertEqual(schedule.stage, 1, 'stage 0 fired, stage 1 armed')

        # Next rung is an hour after creation, measured from the request, not
        # from when the sweep happened to run.
        self.assertAlmostEqual(
            schedule.next_due_at, request.created_at + timedelta(hours=1),
            delta=timedelta(seconds=2),
        )
        push.assert_called_once()
        self.assertEqual(Notification.objects.filter(user=self.user).count(), 1)

    @patch('notifications.reminders.push_device_notification')
    def test_ladder_fires_at_one_hour_then_one_day_then_stops(self, push):
        request = self.make_request()
        base = request.created_at

        # Nothing due 30 minutes in.
        run_reminder_sweep(now=base + timedelta(minutes=30))
        self.assertEqual(HITLReminderSchedule.objects.get(pk=request.reminder_schedule.pk).reminders_sent, 1)

        run_reminder_sweep(now=base + timedelta(hours=1, minutes=1))
        schedule = HITLReminderSchedule.objects.get(hitl_request=request)
        self.assertEqual(schedule.reminders_sent, 2)
        self.assertEqual(schedule.stage, 2)

        run_reminder_sweep(now=base + timedelta(days=1, minutes=1))
        schedule.refresh_from_db()
        self.assertEqual(schedule.reminders_sent, 3)
        self.assertTrue(schedule.is_exhausted)
        self.assertIsNone(schedule.next_due_at, 'exhausted ladder drops out of the index')

        # A week later still nothing more — the ladder is spent, the digest and
        # the optional hourly nag take over from here.
        run_reminder_sweep(now=base + timedelta(days=8))
        schedule.refresh_from_db()
        self.assertEqual(schedule.reminders_sent, 3)

    @patch('notifications.reminders.push_device_notification')
    def test_answering_the_request_cancels_the_ladder(self, push):
        request = self.make_request()
        request.status = 'approved'
        request.responded_at = timezone.now()
        request.save()

        schedule = HITLReminderSchedule.objects.get(hitl_request=request)
        self.assertIsNone(schedule.next_due_at)

        run_reminder_sweep(now=request.created_at + timedelta(days=2))
        schedule.refresh_from_db()
        self.assertEqual(schedule.reminders_sent, 1, 'only the immediate nudge ever went out')

    @patch('notifications.reminders.push_device_notification')
    def test_escalation_can_be_switched_off(self, push):
        prefs = get_preferences(self.user)
        prefs.hitl_escalation_enabled = False
        prefs.save()

        request = self.make_request()
        schedule = HITLReminderSchedule.objects.get(hitl_request=request)
        self.assertEqual(schedule.reminders_sent, 0)
        self.assertIsNone(schedule.next_due_at)
        push.assert_not_called()

    @override_settings(NOTIFICATIONS_EMAIL_ENABLED=True, NOTIFICATIONS_EMAIL_TYPES=[])
    @patch('notifications.utils.send_notification_email')
    @patch('notifications.reminders.push_device_notification')
    def test_escalations_never_send_email_even_with_email_globally_enabled(self, push, send_email):
        # Isolate the ladder: the digest is the one channel allowed to email,
        # and it would otherwise fire during the sweeps below.
        prefs = get_preferences(self.user)
        prefs.daily_digest_enabled = False
        prefs.save()

        request = self.make_request()
        run_reminder_sweep(now=request.created_at + timedelta(hours=1, minutes=1))
        run_reminder_sweep(now=request.created_at + timedelta(days=1, minutes=1))

        self.assertEqual(HITLReminderSchedule.objects.get(hitl_request=request).reminders_sent, 3)
        send_email.assert_not_called()

    @patch('notifications.reminders.push_device_notification')
    def test_quiet_hours_suppress_the_ping_but_not_the_ladder(self, push):
        prefs = get_preferences(self.user)
        prefs.quiet_hours_enabled = True
        prefs.timezone = 'UTC'
        # A window covering the whole day, so the test does not depend on when
        # it runs.
        prefs.quiet_hours_start = timezone.datetime(2020, 1, 1, 0, 0).time()
        prefs.quiet_hours_end = timezone.datetime(2020, 1, 1, 23, 59).time()
        prefs.save()

        request = self.make_request()
        schedule = HITLReminderSchedule.objects.get(hitl_request=request)

        push.assert_not_called()
        self.assertEqual(schedule.reminders_sent, 1, 'ladder still advances')
        self.assertEqual(Notification.objects.filter(user=self.user).count(), 1, 'in-app row still written')


class HourlyReminderTests(ReminderTestCase):
    @patch('notifications.reminders.push_device_notification')
    def test_hourly_is_off_by_default(self, push):
        self.make_request()
        push.reset_mock()

        result = run_reminder_sweep(now=timezone.now() + timedelta(hours=3))
        self.assertEqual(result['hourly'], 0)

    @patch('notifications.reminders.push_device_notification')
    def test_hourly_fires_at_most_once_an_hour_while_something_is_pending(self, push):
        prefs = get_preferences(self.user)
        prefs.hourly_reminders_enabled = True
        prefs.save()

        self.make_request()
        now = timezone.now()

        self.assertEqual(run_reminder_sweep(now=now)['hourly'], 1)
        # Five minutes later the sweep runs again and must not re-nag.
        self.assertEqual(run_reminder_sweep(now=now + timedelta(minutes=5))['hourly'], 0)
        self.assertEqual(run_reminder_sweep(now=now + timedelta(hours=1))['hourly'], 1)

    @patch('notifications.reminders.push_device_notification')
    def test_hourly_stops_once_nothing_is_pending(self, push):
        prefs = get_preferences(self.user)
        prefs.hourly_reminders_enabled = True
        prefs.save()

        request = self.make_request()
        request.status = 'approved'
        request.save()

        self.assertEqual(run_reminder_sweep(now=timezone.now())['hourly'], 0)


class DailyDigestTests(ReminderTestCase):
    def _prefs_at(self, hour, minute=0, tz='UTC'):
        prefs = get_preferences(self.user)
        prefs.daily_digest_enabled = True
        prefs.timezone = tz
        prefs.daily_digest_time = timezone.datetime(2020, 1, 1, hour, minute).time()
        prefs.save()
        return prefs

    @override_settings(NOTIFICATIONS_EMAIL_ENABLED=True, NOTIFICATIONS_EMAIL_TYPES=[])
    @patch('notifications.utils.send_notification_email')
    @patch('notifications.reminders.push_device_notification')
    def test_digest_emails_once_and_only_once_per_day(self, push, send_email):
        self._prefs_at(9)
        self.make_request()

        day = timezone.now().replace(hour=9, minute=5, second=0, microsecond=0)

        self.assertEqual(run_reminder_sweep(now=day)['digests'], 1)
        self.assertEqual(send_email.call_count, 1)

        # Every later sweep the same day is a no-op, however many run.
        for extra in (10, 30, 60, 300):
            run_reminder_sweep(now=day + timedelta(minutes=extra))
        self.assertEqual(send_email.call_count, 1, 'email is capped at one per calendar day')

        # Next day it goes out again.
        self.assertEqual(run_reminder_sweep(now=day + timedelta(days=1))['digests'], 1)
        self.assertEqual(send_email.call_count, 2)

    @override_settings(NOTIFICATIONS_EMAIL_ENABLED=True, NOTIFICATIONS_EMAIL_TYPES=[])
    @patch('notifications.utils.send_notification_email')
    @patch('notifications.reminders.push_device_notification')
    def test_digest_does_not_fire_before_the_chosen_time(self, push, send_email):
        self._prefs_at(17)
        self.make_request()

        morning = timezone.now().replace(hour=8, minute=0, second=0, microsecond=0)
        self.assertEqual(run_reminder_sweep(now=morning)['digests'], 0)
        send_email.assert_not_called()

    @override_settings(NOTIFICATIONS_EMAIL_ENABLED=True, NOTIFICATIONS_EMAIL_TYPES=[])
    @patch('notifications.utils.send_notification_email')
    @patch('notifications.reminders.push_device_notification')
    def test_nothing_pending_means_no_digest_but_the_day_is_still_spent(self, push, send_email):
        prefs = self._prefs_at(9)
        day = timezone.now().replace(hour=9, minute=5, second=0, microsecond=0)

        self.assertEqual(run_reminder_sweep(now=day)['digests'], 0)
        send_email.assert_not_called()

        prefs.refresh_from_db()
        self.assertIsNotNone(prefs.last_digest_sent_on)

        # A request arriving at 22:00 must not trigger a "daily" digest at 22:00.
        self.make_request()
        self.assertEqual(run_reminder_sweep(now=day.replace(hour=22))['digests'], 0)

    @override_settings(NOTIFICATIONS_EMAIL_ENABLED=True, NOTIFICATIONS_EMAIL_TYPES=[])
    @patch('notifications.utils.send_notification_email')
    @patch('notifications.reminders.push_device_notification')
    def test_digest_time_is_interpreted_in_the_users_timezone(self, push, send_email):
        # 09:00 in Kolkata is 03:30 UTC.
        self._prefs_at(9, tz='Asia/Kolkata')
        self.make_request()

        utc_today = timezone.now().replace(hour=2, minute=0, second=0, microsecond=0)
        self.assertEqual(run_reminder_sweep(now=utc_today)['digests'], 0, 'still 07:30 local')
        self.assertEqual(run_reminder_sweep(now=utc_today.replace(hour=4))['digests'], 1)

    @patch('notifications.reminders.push_device_notification')
    def test_unknown_timezone_falls_back_to_utc_without_raising(self, push):
        prefs = get_preferences(self.user)
        prefs.timezone = 'Mars/Olympus_Mons'
        prefs.save()
        self.make_request()

        run_reminder_sweep(now=timezone.now())  # must not raise


class PreferenceApiTests(ReminderTestCase):
    def setUp(self):
        super().setUp()
        self.api = APIClient()
        self.api.force_authenticate(user=self.user)

    def test_preferences_are_created_on_first_read(self):
        response = self.api.get('/api/notifications/preferences/')

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()['daily_digest_enabled'])
        self.assertTrue(NotificationPreference.objects.filter(user=self.user).exists())

    def test_client_cannot_reset_the_daily_email_cap(self):
        prefs = get_preferences(self.user)
        prefs.last_digest_sent_on = timezone.now().date()
        prefs.save()

        response = self.api.patch(
            '/api/notifications/preferences/',
            {'last_digest_sent_on': None, 'hourly_reminders_enabled': True},
            format='json',
        )

        self.assertEqual(response.status_code, 200)
        prefs.refresh_from_db()
        self.assertIsNotNone(prefs.last_digest_sent_on, 'read-only field held')
        self.assertTrue(prefs.hourly_reminders_enabled, 'writable field applied')

    def test_invalid_timezone_is_rejected(self):
        response = self.api.patch(
            '/api/notifications/preferences/',
            {'timezone': 'Not/AZone'},
            format='json',
        )
        self.assertEqual(response.status_code, 400)


class AbandonmentTests(ReminderTestCase):
    """The backstop that stops a request nobody will ever answer.

    Without it a pending request is immortal: it escalates, joins every daily
    digest for ever, and — once triggers exist — holds its run open while the
    next scheduled tick starts another one on top of it.
    """

    def test_a_long_unanswered_request_is_cancelled(self):
        request = self.make_request()
        HITLRequest.objects.filter(pk=request.pk).update(
            created_at=timezone.now() - timedelta(days=8)
        )

        result = run_reminder_sweep()

        request.refresh_from_db()
        self.assertEqual(result['abandoned'], 1)
        self.assertEqual(request.status, 'cancelled')
        self.assertIsNotNone(request.responded_at)

    def test_a_recent_request_is_left_alone(self):
        """The default `timeout_seconds` is 300s.

        Honouring it would cancel every request five minutes in — before the
        +1h nudge the escalation ladder exists to send. This test is what fails
        if someone wires `timeout_seconds` into the sweep without deciding what
        it should mean alongside the ladder.
        """
        request = self.make_request()
        HITLRequest.objects.filter(pk=request.pk).update(
            created_at=timezone.now() - timedelta(hours=2)
        )

        result = run_reminder_sweep()

        request.refresh_from_db()
        self.assertEqual(result['abandoned'], 0)
        self.assertEqual(request.status, 'pending')

    def test_an_answered_request_is_not_touched(self):
        request = self.make_request()
        HITLRequest.objects.filter(pk=request.pk).update(
            status='approved', created_at=timezone.now() - timedelta(days=30)
        )

        run_reminder_sweep()

        request.refresh_from_db()
        self.assertEqual(request.status, 'approved')

    @override_settings(HITL_ABANDON_AFTER_DAYS=1)
    def test_the_window_is_configurable(self):
        # Read at import time, so the setting is re-read rather than assumed
        # live — this documents which of the two it is.
        from notifications import reminders

        request = self.make_request()
        HITLRequest.objects.filter(pk=request.pk).update(
            created_at=timezone.now() - reminders.ABANDON_AFTER - timedelta(minutes=1)
        )

        run_reminder_sweep()

        request.refresh_from_db()
        self.assertEqual(request.status, 'cancelled')

    def test_a_cancelled_request_is_not_also_nudged_in_the_same_sweep(self):
        request = self.make_request()
        HITLRequest.objects.filter(pk=request.pk).update(
            created_at=timezone.now() - timedelta(days=8)
        )
        HITLReminderSchedule.objects.filter(hitl_request=request).update(
            next_due_at=timezone.now() - timedelta(minutes=1)
        )

        result = run_reminder_sweep()

        self.assertEqual(result['abandoned'], 1)
        self.assertEqual(result['escalations'], 0)
