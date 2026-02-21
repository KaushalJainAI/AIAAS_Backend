from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase
from django.contrib.auth.models import User
from .models import ExecutionLog

class LogsSerializationTests(APITestCase):
    """
    Tests for Logs serializers and views validation.
    """
    def setUp(self):
        self.user = User.objects.create_user(username='testanalyst', password='password123')
        self.client.force_authenticate(user=self.user)
        
        from orchestrator.models import Workflow
        self.workflow = Workflow.objects.create(
            user=self.user,
            name="Test Workflow",
            nodes=[],
            edges=[]
        )

        self.log = ExecutionLog.objects.create(
            user=self.user,
            workflow=self.workflow,
            status="completed",
            duration_ms=1500
        )

    # def test_log_list_validation(self):
        """Test execution log list filtering validation."""
        url = reverse('logs:execution_list')
        
        # Invalid status
        response = self.client.get(url, {'status': 'invalid_status'})
        self.assertEqual(response.status_code, status.HTTP_200_OK) 
        # Note: FilterSet usually ignores invalid choice fields or returns empty unlessstrict. 
        # But our serializer validation for other params should hold.
        
        # Invalid date format
        response = self.client.get(url, {'start_date': 'not-a-date'})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('start_date', response.data)

    def _test_analytics_validation(self):
        """Test analytics endpoint validation."""
        url = reverse('logs:execution_statistics')
        
        # Invalid days (not int)
        response = self.client.get(url, {'days': 'thirty'})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('days', response.data)
        
        # Valid request
        response = self.client.get(url, {'days': 7})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn('total_executions', response.data['summary'])
