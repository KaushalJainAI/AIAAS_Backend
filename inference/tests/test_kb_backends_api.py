"""
KB routes removed — KB is internal, no HTTP CRUD.

The old surface (GET/POST /api/inference/kbs/, GET/PATCH /api/inference/kbs/<id>/)
exposed the storage container as a user-manageable object. Deleting or recreating
a KB orphans vectors/postings that backends hold, and the agent's `knowledgeBases`
attachment becomes a cross-tenant read if the view is not perfectly scoped.
The container is now internal: one implicit Default per user, auto-created on
first upload. These tests lock that in — any CRUD probe must 404.
"""
from __future__ import annotations

from django.contrib.auth.models import User
from rest_framework import status
from rest_framework.test import APITestCase

from inference.models import KnowledgeBase


class KnowledgeBaseRoutesRemovedTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='kbowner', password='pw12345')
        self.client.force_authenticate(user=self.user)
        self.kb = KnowledgeBase.objects.create(user=self.user, name='Mine')

    def test_list_returns_404(self):
        response = self.client.get('/api/inference/kbs/')
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_create_returns_404(self):
        response = self.client.post('/api/inference/kbs/', {'name': 'Grep corpus', 'backend': 'fulltext'}, format='json')
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_detail_returns_404(self):
        response = self.client.get(f'/api/inference/kbs/{self.kb.id}/')
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_patch_returns_404(self):
        response = self.client.patch(f'/api/inference/kbs/{self.kb.id}/', {'backend': 'raw'}, format='json')
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_delete_returns_404(self):
        response = self.client.delete(f'/api/inference/kbs/{self.kb.id}/')
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
