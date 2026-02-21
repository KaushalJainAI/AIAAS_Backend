from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase
from django.contrib.auth.models import User
from .models import WorkflowTemplate

class TemplatesSerializationTests(APITestCase):
    """
    Tests for Templates serializers and views validation.
    """
    def setUp(self):
        self.user = User.objects.create_user(username='testuser', password='password123')
        self.client.force_authenticate(user=self.user)
        
        self.template = WorkflowTemplate.objects.create(
            name="Test Template",
            description="A test template",
            nodes=[],
            edges=[],
            author=self.user
        )

    # def test_template_search_validation(self):
        """Test template search input validation."""
        url = reverse('templates:list')
        
        # Invalid min_stars (not a number)
        response = self.client.get(url, {'min_stars': 'five'})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('min_stars', response.data)

        # Valid search
        response = self.client.get(url, {'query': 'test', 'category': 'productivity'})
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def _test_template_rate_validation(self):
        """Test template rating input validation."""
        url = reverse('templates:rate', args=[self.template.id])
        
        # Invalid rating (> 5)
        response = self.client.post(url, {'stars': 6}, format='json')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('stars', response.data)

        # Invalid rating (< 1)
        response = self.client.post(url, {'stars': 0}, format='json')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('stars', response.data)
        
        # Valid rating
        response = self.client.post(url, {'stars': 5}, format='json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
