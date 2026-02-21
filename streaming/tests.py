from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase
from django.contrib.auth.models import User
from .models import StreamEvent
from logs.models import ExecutionLog

class StreamingSerializationTests(APITestCase):
    """
    Tests for Streaming serializers and views validation.
    """
    def setUp(self):
        self.user = User.objects.create_user(username='teststreamer', password='password123')
        self.client.force_authenticate(user=self.user)
        
        from orchestrator.models import Workflow
        self.workflow = Workflow.objects.create(
            user=self.user,
            name="Stream Workflow",
            nodes=[],
            edges=[]
        )
        
        self.execution = ExecutionLog.objects.create(
            user=self.user,
            workflow=self.workflow,
            status="running"
        )
        
        StreamEvent.objects.create(
            user=self.user,
            execution=self.execution,
            sequence=1,
            event_type="test",
            data={}
        )

    def test_history_validation(self):
        """Test streaming history input validation."""
        url = reverse('streaming:execution-events', args=[self.execution.execution_id])
        
        # Invalid limit (too high)
        response = self.client.get(url, {'limit': 1000})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('limit', response.data)

        # Invalid after_sequence (negative)
        response = self.client.get(url, {'after_sequence': -1})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('after_sequence', response.data)

        # Valid request
        response = self.client.get(url, {'limit': 10, 'after_sequence': 0})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data['events']), 1)
