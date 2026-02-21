from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase
from django.contrib.auth.models import User
from .models import Document

class InferenceSerializationTests(APITestCase):
    """
    Tests for Inference serializers and views validation.
    """
    def setUp(self):
        self.user = User.objects.create_user(username='testresearcher', password='password123')
        self.client.force_authenticate(user=self.user)
        
        self.doc = Document.objects.create(
            user=self.user,
            name="Manual.pdf",
            file_type="pdf",
            file_size=1024,
            status="completed",
            content_text="This is the manual content."
        )

    def test_document_list_serialization(self):
        """Test document list serialization parity."""
        url = reverse('inference:document_list')
        response = self.client.get(url)
        
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        my_docs = response.data['my_documents']
        self.assertEqual(len(my_docs), 1)
        
        # Verify mapped fields
        self.assertEqual(my_docs[0]['title'], "Manual.pdf")
        self.assertEqual(my_docs[0]['filename'], "Manual.pdf")
        self.assertEqual(my_docs[0]['content'], "This is the manual content.")
        self.assertEqual(my_docs[0]['author_name'], "testresearcher")

    def test_rag_search_validation(self):
        """Test RAG search input validation (400 Bad Request)."""
        url = reverse('inference:rag_search')
        
        # Missing 'query'
        invalid_data = {
            'top_k': 10
        }
        response = self.client.post(url, invalid_data, format='json')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('query', response.data)

    def test_rag_query_validation(self):
        """Test RAG query input validation (400 Bad Request)."""
        url = reverse('inference:rag_query')
        
        # Invalid top_k (string instead of int)
        invalid_data = {
            'question': 'How many documents?',
            'top_k': 'lots'
        }
        response = self.client.post(url, invalid_data, format='json')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('top_k', response.data)
