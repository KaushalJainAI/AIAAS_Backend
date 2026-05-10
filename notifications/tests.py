from unittest.mock import patch
from django.contrib.auth.models import User
from django.test import TestCase, override_settings

from .models import Notification
from .utils import create_notification, should_send_email_notification


class EmailNotificationTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username='notify-user',
            email='notify@example.com',
            password='password123'
        )

    @override_settings(NOTIFICATIONS_EMAIL_ENABLED=True, NOTIFICATIONS_EMAIL_TYPES=[])
    @patch('notifications.utils.send_notification_email')
    def test_create_notification_sends_email_when_enabled(self, mock_send_email):
        notification = create_notification(
            user=self.user,
            type='system',
            title='System update',
            message='A system event happened.',
        )

        self.assertIsInstance(notification, Notification)
        mock_send_email.assert_called_once_with(notification)

    @override_settings(NOTIFICATIONS_EMAIL_ENABLED=True, NOTIFICATIONS_EMAIL_TYPES=['workflow_failed'])
    @patch('notifications.utils.send_notification_email')
    def test_email_type_allowlist_is_respected(self, mock_send_email):
        create_notification(
            user=self.user,
            type='system',
            title='System update',
            message='This should stay in-app only.',
        )

        mock_send_email.assert_not_called()

    @override_settings(NOTIFICATIONS_EMAIL_ENABLED=True, NOTIFICATIONS_EMAIL_TYPES=[])
    def test_notification_data_can_suppress_email(self):
        notification = Notification.objects.create(
            user=self.user,
            type='system',
            title='System update',
            message='This should stay in-app only.',
            data={'send_email': False},
        )

        self.assertFalse(should_send_email_notification(notification))
