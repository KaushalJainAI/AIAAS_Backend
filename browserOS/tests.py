from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase
from django.contrib.auth import get_user_model
from .models import OSWorkspace, OSAppWindow, OSNotification

User = get_user_model()

class BrowserOSTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='testuser', password='password')
        self.other_user = User.objects.create_user(username='other', password='password')
        self.client.force_authenticate(user=self.user)
        
    def test_workspace_creation_and_sandboxing(self):
        # Create default workspace
        url = reverse('osworkspace-mine')
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        
        # Check if created
        workspace = OSWorkspace.objects.get(user=self.user)
        self.assertEqual(workspace.name, "My Workspace")
        
        # Ensure we only see our own
        OSWorkspace.objects.create(user=self.other_user, name="Other's Workspace")
        list_url = reverse('osworkspace-list')
        list_response = self.client.get(list_url)
        
        # Handle pagination
        results = list_response.data.get('results', list_response.data)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]['name'], "My Workspace")

    def test_app_window_crud(self):
        # Create window
        url = reverse('osappwindow-list')
        data = {
            "app_id": "ai_notes",
            "title": "My Note",
            "position_x": 200,
            "position_y": 150
        }
        response = self.client.post(url, data, format='json')
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        
        # Update window
        window_id = response.data['id']
        update_url = reverse('osappwindow-detail', args=[window_id])
        update_data = {"position_x": 500, "title": "Updated Note"}
        update_response = self.client.patch(update_url, update_data, format='json')
        self.assertEqual(update_response.status_code, status.HTTP_200_OK)
        self.assertEqual(update_response.data['position_x'], 500)

    def test_notification_mark_all_read(self):
        OSNotification.objects.create(user=self.user, title="Hello", message="Test", is_read=False)
        OSNotification.objects.create(user=self.user, title="World", message="Test 2", is_read=False)
        
        url = reverse('osnotification-mark-all-read')
        response = self.client.post(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        
        self.assertEqual(OSNotification.objects.filter(user=self.user, is_read=False).count(), 0)
