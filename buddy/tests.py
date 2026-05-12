from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from browserOS.models import OSAppWindow, OSNotification, OSWorkspace

User = get_user_model()


class BuddyCommandTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="buddy-user", password="password")
        self.client.force_authenticate(user=self.user)

    def test_text_command_opens_browseros_app(self):
        response = self.client.post(
            reverse("process_command"),
            {"command": "open terminal"},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["action_details"]["type"], "os_open_app")

        workspace = OSWorkspace.objects.get(user=self.user)
        window = OSAppWindow.objects.get(workspace=workspace, app_id="terminal")
        self.assertEqual(window.title, "Terminal")
        self.assertFalse(window.is_minimized)

    def test_text_command_creates_notification(self):
        response = self.client.post(
            reverse("process_command"),
            {"command": 'show notification "Build finished successfully"'},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["action_details"]["type"], "os_notify")
        self.assertTrue(
            OSNotification.objects.filter(
                user=self.user,
                message="Build finished successfully",
            ).exists()
        )

    def test_explicit_action_still_supported(self):
        response = self.client.post(
            reverse("trigger_action"),
            {"action_type": "os_open_app", "parameters": {"app_id": "calculator"}},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["action_details"]["type"], "os_open_app")
        self.assertTrue(
            OSAppWindow.objects.filter(workspace__user=self.user, app_id="calculator").exists()
        )

    @patch("buddy.views._find_browser_tool")
    @patch("buddy.views._send_action_event")
    def test_browser_command_resolves_to_navigation(self, mock_send_action_event, mock_find_browser_tool):
        mock_find_browser_tool.return_value = None

        response = self.client.post(
            reverse("process_command"),
            {"command": "navigate to https://example.com"},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["action_details"]["type"], "browser_navigate")
        self.assertEqual(
            response.data["action_details"]["details"]["status"],
            "pending_frontend",
        )
        mock_send_action_event.assert_called_once()

    def test_unknown_command_returns_helpful_error(self):
        response = self.client.post(
            reverse("process_command"),
            {"command": "do the thing somehow"},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("supported_examples", response.data)
