
from django.contrib.auth.models import User
from rest_framework import status
from rest_framework.test import APITestCase, APIClient
import json
from uuid import uuid4

class PartialExecutionTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='testuser', password='password')
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)
        self.url = '/api/orchestrator/workflows/execute_partial/'

    def test_missing_fields(self):
        """Test with missing node_id and node_type."""
        response = self.client.post(self.url, data={}, format='json')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        # Standardized to 'detail' for frontend compatibility
        self.assertEqual(response.data.get('detail'), 'node_id and node_type are required')

    def test_unknown_node_type(self):
        """Test with unknown node_type."""
        payload = {
            "node_id": "test-node",
            "node_type": "unknown_type"
        }
        response = self.client.post(self.url, data=payload, format='json')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('Unknown node type', response.data.get('detail', ''))

    def test_valid_code_node(self):
        """Test with a valid code node."""
        payload = {
            "node_id": "test-code-node",
            "node_type": "code",
            "input_data": {"test": "data"},
            "config": {"code": "return {'result': data['test'] + ' processed'}"}
        }
        response = self.client.post(self.url, data=payload, format='json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn('items', response.data)
        self.assertEqual(response.data['items'][0]['json']['result'], 'data processed')

    def test_workflow_id_propagation(self):
        """Test workflow_id propagation."""
        payload = {
            "workflow_id": 123,
            "node_id": "test-workflow-id",
            "node_type": "code",
            "input_data": {},
            "config": {"code": "return {'wf_id': context['workflow_id']}"}
        }
        response = self.client.post(self.url, data=payload, format='json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['items'][0]['json']['wf_id'], 123)

    def test_code_error_standardization(self):
        """Test that code errors also return 'detail'."""
        payload = {
            "node_id": "test-error-node",
            "node_type": "code",
            "input_data": {},
            "config": {"code": "raise Exception('Custom error message')"}
        }
        response = self.client.post(self.url, data=payload, format='json')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.data.get('detail'), 'Code execution failed: Custom error message')
