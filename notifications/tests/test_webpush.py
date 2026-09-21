"""Web Push (closed-browser OS notifications): subscribe flow + send gating."""
import unittest
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from notifications.models import PushSubscription
from notifications.webpush import is_configured, send_web_push

try:
    import pywebpush  # noqa: F401
    HAS_PYWEBPUSH = True
except ImportError:
    HAS_PYWEBPUSH = False


def _sub(user, endpoint='https://push.example.com/sub/1'):
    return PushSubscription.objects.create(
        user=user, endpoint=endpoint, p256dh='p256dh-key', auth='auth-key',
    )


class VapidKeyTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='pusher', password='pw')
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    def test_vapid_key_reports_disabled_without_keys(self):
        response = self.client.get('/api/notifications/push/vapid-key/')
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.data['enabled'])

    @override_settings(VAPID_PUBLIC_KEY='pub', VAPID_PRIVATE_KEY='priv')
    def test_vapid_key_reports_enabled_with_keys(self):
        response = self.client.get('/api/notifications/push/vapid-key/')
        self.assertTrue(response.data['enabled'])
        self.assertEqual(response.data['public_key'], 'pub')

    def test_vapid_key_requires_auth(self):
        anon = APIClient()
        self.assertEqual(anon.get('/api/notifications/push/vapid-key/').status_code, 401)


class SubscribeTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='sub', password='pw')
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)
        self.payload = {
            'endpoint': 'https://push.example.com/sub/abc',
            'p256dh': 'p256dh-value',
            'auth': 'auth-value',
            'user_agent': 'TestBrowser/1.0',
        }

    def test_subscribe_stores_a_row(self):
        response = self.client.post('/api/notifications/push/subscribe/', self.payload, format='json')
        self.assertEqual(response.status_code, 201)
        self.assertTrue(PushSubscription.objects.filter(
            user=self.user, endpoint=self.payload['endpoint']).exists())

    def test_resubscribe_upserts_rather_than_duplicating(self):
        self.client.post('/api/notifications/push/subscribe/', self.payload, format='json')
        changed = dict(self.payload, p256dh='rotated-key')
        response = self.client.post('/api/notifications/push/subscribe/', changed, format='json')
        self.assertEqual(response.status_code, 201)
        rows = PushSubscription.objects.filter(endpoint=self.payload['endpoint'])
        self.assertEqual(rows.count(), 1)
        self.assertEqual(rows.first().p256dh, 'rotated-key')

    def test_unsubscribe_removes_the_row(self):
        _sub(self.user, self.payload['endpoint'])
        response = self.client.post(
            '/api/notifications/push/unsubscribe/',
            {'endpoint': self.payload['endpoint']}, format='json')
        self.assertEqual(response.status_code, 200)
        self.assertFalse(PushSubscription.objects.filter(
            user=self.user, endpoint=self.payload['endpoint']).exists())

    def test_unsubscribe_unknown_endpoint_is_still_200(self):
        response = self.client.post(
            '/api/notifications/push/unsubscribe/',
            {'endpoint': 'https://push.example.com/nope'}, format='json')
        self.assertEqual(response.status_code, 200)

    def test_a_second_user_cannot_remove_the_first_users_row(self):
        _sub(self.user, self.payload['endpoint'])
        stranger = User.objects.create_user(username='stranger', password='pw')
        client = APIClient()
        client.force_authenticate(user=stranger)
        client.post('/api/notifications/push/unsubscribe/',
                    {'endpoint': self.payload['endpoint']}, format='json')
        self.assertTrue(PushSubscription.objects.filter(
            user=self.user, endpoint=self.payload['endpoint']).exists())


class SendGatingTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='send', password='pw')

    def test_unconfigured_sends_nothing(self):
        _sub(self.user)
        self.assertFalse(is_configured())
        self.assertEqual(send_web_push(self.user, title='t', body='b'), 0)

    def test_no_subscriptions_sends_nothing(self):
        with override_settings(VAPID_PUBLIC_KEY='pub', VAPID_PRIVATE_KEY='priv'):
            self.assertEqual(send_web_push(self.user, title='t', body='b'), 0)

    @override_settings(VAPID_PUBLIC_KEY='pub', VAPID_PRIVATE_KEY='priv')
    @unittest.skipUnless(HAS_PYWEBPUSH, 'pywebpush not installed')
    @patch('notifications.webpush.webpush')
    def test_send_reaches_each_subscription(self, mock_webpush):
        _sub(self.user, 'https://push.example.com/1')
        _sub(self.user, 'https://push.example.com/2')
        self.assertEqual(send_web_push(self.user, title='t', body='b'), 2)
        self.assertEqual(mock_webpush.call_count, 2)

    @override_settings(VAPID_PUBLIC_KEY='pub', VAPID_PRIVATE_KEY='priv')
    @unittest.skipUnless(HAS_PYWEBPUSH, 'pywebpush not installed')
    @patch('notifications.webpush.webpush')
    def test_expired_endpoint_is_pruned(self, mock_webpush):
        from pywebpush import WebPushException

        exc = WebPushException('gone')
        exc.response = type('Resp', (), {'status_code': 410})()
        mock_webpush.side_effect = exc
        sub = _sub(self.user)
        self.assertEqual(send_web_push(self.user, title='t', body='b'), 0)
        self.assertFalse(PushSubscription.objects.filter(pk=sub.pk).exists())


class EscalationSendsWebPushTests(TestCase):
    """The ladder's device gate now carries the closed-browser twin."""

    @override_settings(VAPID_PUBLIC_KEY='pub', VAPID_PRIVATE_KEY='priv')
    @unittest.skipUnless(HAS_PYWEBPUSH, 'pywebpush not installed')
    @patch('notifications.webpush.webpush')
    @patch('notifications.reminders.push_device_notification')
    def test_notify_device_sends_web_push(self, _socket, mock_webpush):
        from notifications.models import NotificationPreference
        from notifications.reminders import _notify_device

        prefs = NotificationPreference.objects.create(user=self.user)
        _sub(self.user)
        _notify_device(prefs, notif_type='hitl_request', title='Agent needs you',
                       message='Approve?', data={'action_url': '/inbox'})
        self.assertEqual(mock_webpush.call_count, 1)

    @patch('notifications.webpush.webpush')
    @patch('notifications.reminders.push_device_notification')
    def test_device_toggle_off_suppresses_web_push(self, _socket, mock_webpush):
        from notifications.models import NotificationPreference
        from notifications.reminders import _notify_device

        prefs = NotificationPreference.objects.create(
            user=self.user, device_notifications_enabled=False)
        _sub(self.user)
        with override_settings(VAPID_PUBLIC_KEY='pub', VAPID_PRIVATE_KEY='priv'):
            _notify_device(prefs, notif_type='hitl_request', title='t',
                           message='m', data={'action_url': '/inbox'})
        mock_webpush.assert_not_called()

    def setUp(self):
        self.user = User.objects.create_user(username='ladder', password='pw')
