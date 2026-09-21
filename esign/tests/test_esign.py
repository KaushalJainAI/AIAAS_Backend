"""
The e-sign webhook: every refusal is the same 404, completion notifies.
"""
from __future__ import annotations

import json

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from esign.models import SignatureRequest
from notifications.models import Notification

User = get_user_model()


class SignatureWebhookTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user('signer', 's@example.com', 'pw')
        self.row = SignatureRequest.objects.create(
            user=self.user, path='/Chat/offer.pdf',
            signers=[{'name': 'Asha', 'email': 'asha@example.com'}],
            status='sent', provider_request_id='prov-1')

    def post(self, secret, body):
        return self.client.post(
            reverse('esign:signature_hook', args=[secret]),
            data=json.dumps(body), content_type='application/json')

    def test_completion_closes_and_notifies(self):
        response = self.post(self.row.secret, {'event': 'completed'})
        self.assertEqual(response.status_code, 200)
        self.row.refresh_from_db()
        self.assertEqual(self.row.status, 'completed')
        self.assertIsNotNone(self.row.completed_at)
        self.assertTrue(Notification.objects.filter(
            user=self.user, type='agent_update').exists())

    def test_a_decline_is_recorded(self):
        response = self.post(self.row.secret, {'event': 'declined'})
        self.assertEqual(response.status_code, 200)
        self.row.refresh_from_db()
        self.assertEqual(self.row.status, 'declined')

    def test_every_refusal_is_the_same_404(self):
        bodies = [
            (self.row.secret, {'event': 'something-else'}),
            ('wrong-secret', {'event': 'completed'}),
        ]
        for secret, body in bodies:
            response = self.post(secret, body)
            self.assertEqual(response.status_code, 404, body)
            self.assertEqual(response.json(), {'detail': 'Not found.'})

    def test_a_finished_request_cannot_be_reopened(self):
        self.post(self.row.secret, {'event': 'completed'})
        response = self.post(self.row.secret, {'event': 'declined'})
        self.assertEqual(response.status_code, 404)
        self.row.refresh_from_db()
        self.assertEqual(self.row.status, 'completed')

    def test_get_is_refused_like_everything_else(self):
        response = self.client.get(reverse('esign:signature_hook', args=[self.row.secret]))
        self.assertEqual(response.status_code, 404)
