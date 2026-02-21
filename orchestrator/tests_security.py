from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase
from django.contrib.auth.models import User
from uuid import uuid4
from unittest.mock import patch, MagicMock
import asyncio

class ExecutionControlSecurityTests(APITestCase):
    """
    Tests for security and error handling in execution control endpoints.
    """
    def setUp(self):
        self.user1 = User.objects.create_user(username='user1', password='password123')
        self.user2 = User.objects.create_user(username='user2', password='password123')
        self.execution_id = str(uuid4())

    def test_unauthorized_pause(self):
        """Test pausing another user's execution (should return 404)."""
        self.client.force_authenticate(user=self.user2)
        url = reverse('orchestrator:pause_execution', args=[self.execution_id])
        
        # Mock king to raise AuthorizationError
        with patch('executor.king.KingOrchestrator._check_execution_auth') as mock_auth:
            from executor.king import AuthorizationError
            mock_auth.side_effect = AuthorizationError("Not authorized")
            
            response = self.client.post(url)
            self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
            self.assertEqual(response.data['error'], 'Execution not found or not authorized')

    def test_conflict_pause_already_paused(self):
        """Test pausing an already paused execution (should return 409)."""
        self.client.force_authenticate(user=self.user1)
        url = reverse('orchestrator:pause_execution', args=[self.execution_id])
        
        with patch('executor.king.KingOrchestrator.pause') as mock_pause:
            from executor.king import StateConflictError
            mock_pause.side_effect = StateConflictError("Execution is already paused")
            
            response = self.client.post(url)
            self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
            self.assertEqual(response.data['error'], 'Execution is already paused')

    def test_ghost_cleanup_pause(self):
        """Test pausing a ghost execution (should return 409 and clean up)."""
        self.client.force_authenticate(user=self.user1)
        url = reverse('orchestrator:pause_execution', args=[self.execution_id])
        
        with patch('executor.king.KingOrchestrator.pause') as mock_pause:
            from executor.king import StateConflictError
            mock_pause.side_effect = StateConflictError("Execution has already terminated")
            
            response = self.client.post(url)
            self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
            self.assertIn('terminated', response.data['error'])

    def test_invalid_uuid_pause(self):
        """Test pausing with an invalid UUID format (should return 400)."""
        self.client.force_authenticate(user=self.user1)
        url = reverse('orchestrator:pause_execution', args=['not-a-uuid'])
        
        response = self.client.post(url)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.data['error'], 'Invalid execution ID format')

    def test_unauthorized_resume(self):
        """Test resuming another user's execution (should return 404)."""
        self.client.force_authenticate(user=self.user2)
        url = reverse('orchestrator:resume_execution', args=[self.execution_id])
        
        with patch('executor.king.KingOrchestrator.resume') as mock_resume:
            from executor.king import AuthorizationError
            mock_resume.side_effect = AuthorizationError("Not authorized")
            
            response = self.client.post(url)
            self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_unauthorized_stop(self):
        """Test stopping another user's execution (should return 404)."""
        self.client.force_authenticate(user=self.user2)
        url = reverse('orchestrator:stop_execution', args=[self.execution_id])
        
        with patch('executor.king.KingOrchestrator.stop') as mock_stop:
            from executor.king import AuthorizationError
            mock_stop.side_effect = AuthorizationError("Not authorized")
            
            response = self.client.post(url)
            self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_unauthorized_status(self):
        """Test getting status of another user's execution (should return 404)."""
        self.client.force_authenticate(user=self.user2)
        url = reverse('orchestrator:execution_status', args=[self.execution_id])
        
        with patch('executor.king.KingOrchestrator.get_status') as mock_status:
            mock_status.return_value = None # King returns None for unauthorized status
            
            response = self.client.get(url)
            self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
