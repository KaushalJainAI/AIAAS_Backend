"""
User-asked reminders through the tool surface.

Pinned: timing is quoted, never chosen — the past, the unparseable and the
absurdly far are refused with the reason; the per-user live cap holds;
listing and cancelling are owner-only; and a stored link that becomes a UI
link is an in-app path or nothing (the open-redirect rule `notify_user`
already follows).
"""
from __future__ import annotations

import json
from datetime import timedelta

from asgiref.sync import async_to_sync
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from chat.tools import execute_tool
from notifications.models import ScheduledNotification

User = get_user_model()


class ReminderToolTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user('owner', 'o@example.com', 'pw')
        self.other = User.objects.create_user('other', 't@example.com', 'pw')
        self.ctx = {'user_id': self.user.id}

    def call(self, name, args, user=None):
        ctx = {'user_id': (user or self.user).id}
        return json.loads(async_to_sync(execute_tool)(name, args, ctx))

    def future(self, **kwargs):
        return (timezone.now() + timedelta(**kwargs)).isoformat()

    def test_schedule_once(self):
        out = self.call('schedule_notification', {
            'title': 'Standup notes', 'message': 'Send the notes.',
            'run_at': self.future(hours=2), 'link': '/runs'})
        self.assertTrue(out['scheduled'])
        self.assertEqual(out['repeat'], 'none')
        row = ScheduledNotification.objects.get(user=self.user)
        self.assertEqual(row.data, {'action_url': '/runs'})

    def test_schedule_heartbeat(self):
        out = self.call('schedule_notification', {
            'title': 'Heartbeat', 'message': 'Ping.',
            'run_at': self.future(minutes=30), 'repeat': 'daily'})
        self.assertTrue(out['scheduled'])
        self.assertEqual(
            ScheduledNotification.objects.get(user=self.user).repeat, 'daily')

    def test_past_unparseable_and_absurd_are_refused(self):
        past = self.call('schedule_notification', {
            'title': 'T', 'message': 'M',
            'run_at': (timezone.now() - timedelta(hours=1)).isoformat()})
        self.assertIn('past', past['error'])
        bad = self.call('schedule_notification', {
            'title': 'T', 'message': 'M', 'run_at': 'someday-ish'})
        self.assertIn('error', bad)
        far = self.call('schedule_notification', {
            'title': 'T', 'message': 'M',
            'run_at': (timezone.now() + timedelta(days=400)).isoformat()})
        self.assertIn('year', far['error'])
        wrong = self.call('schedule_notification', {
            'title': 'T', 'message': 'M',
            'run_at': self.future(hours=1), 'repeat': 'fortnightly'})
        self.assertIn('error', wrong)
        self.assertEqual(ScheduledNotification.objects.count(), 0)

    def test_naive_time_is_read_in_the_users_timezone(self):
        out = self.call('schedule_notification', {
            'title': 'T', 'message': 'M', 'run_at': '2026-09-24T09:00:00'})
        self.assertTrue(out['scheduled'])
        row = ScheduledNotification.objects.get(user=self.user)
        self.assertTrue(row.next_run_at.tzinfo is not None)

    def test_external_link_is_dropped_not_stored(self):
        out = self.call('schedule_notification', {
            'title': 'T', 'message': 'M',
            'run_at': self.future(hours=1),
            'link': 'https://evil.example/x'})
        self.assertTrue(out['scheduled'])
        row = ScheduledNotification.objects.get(user=self.user)
        self.assertEqual(row.data, {})

    def test_live_cap_is_enforced(self):
        now = timezone.now()
        for i in range(ScheduledNotification.MAX_ACTIVE_PER_USER):
            ScheduledNotification.objects.create(
                user=self.user, title=f'R{i}', message='M',
                next_run_at=now + timedelta(hours=i + 1))
        out = self.call('schedule_notification', {
            'title': 'One more', 'message': 'M',
            'run_at': self.future(hours=30)})
        self.assertIn('limit', out['error'])

    def test_list_and_cancel_are_owner_only(self):
        mine = self.call('schedule_notification', {
            'title': 'Mine', 'message': 'M', 'run_at': self.future(hours=1)})
        listed = self.call('list_scheduled_notifications', {},
                           user=self.other)
        self.assertEqual(listed['count'], 0)
        refused = self.call('cancel_scheduled_notification',
                            {'id': mine['id']}, user=self.other)
        self.assertIn('No live reminder', refused['error'])
        done = self.call('cancel_scheduled_notification', {'id': mine['id']})
        self.assertTrue(done['cancelled'])
        row = ScheduledNotification.objects.get(id=mine['id'])
        self.assertFalse(row.active)
        self.assertIsNone(row.next_run_at)
        again = self.call('cancel_scheduled_notification', {'id': mine['id']})
        self.assertIn('error', again)

    def test_list_shows_when_and_whether_it_repeats(self):
        self.call('schedule_notification', {
            'title': 'Morning', 'message': 'Standup.',
            'run_at': self.future(hours=12), 'repeat': 'daily'})
        out = self.call('list_scheduled_notifications', {})
        self.assertEqual(out['count'], 1)
        self.assertEqual(out['reminders'][0]['repeat'], 'daily')
        self.assertIn('next_run_at', out['reminders'][0])

    def test_email_is_opt_in_and_defaults_off(self):
        plain = self.call('schedule_notification', {
            'title': 'T', 'message': 'M', 'run_at': self.future(hours=1)})
        self.assertFalse(plain['email'])
        loud = self.call('schedule_notification', {
            'title': 'T2', 'message': 'M', 'run_at': self.future(hours=2),
            'email': True})
        self.assertTrue(loud['email'])
        self.assertTrue(
            ScheduledNotification.objects.get(id=loud['id']).send_email)
