from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase
from django.contrib.auth.models import User
from .models import Skill

class SkillsSerializationTests(APITestCase):
    """
    Tests for Skills serializers and views validation.
    """
    def setUp(self):
        self.user = User.objects.create_user(username='testcreator', password='password123')
        self.client.force_authenticate(user=self.user)

    def test_skill_search_validation(self):
        """Test skill search input validation."""
        url = reverse('skill-search')
        
        # Invalid tab (not 'mine' or 'public')
        response = self.client.get(url, {'tab': 'invalid'})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('tab', response.data)

        # Invalid page (not an integer)
        response = self.client.get(url, {'page': 'first'})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('page', response.data)

        # Valid search
        response = self.client.get(url, {'query': 'test', 'tab': 'public'})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn('results', response.data)
