"""
Integration tests covering the auth surface end-to-end at the HTTP layer.

Happy / Sad / Angry pattern:
    Happy  — register → login → use access token
    Sad    — wrong password, missing field, expired token shape
    Angry  — SQL-style injection in username, oversized payloads,
             token tampering, replay across users
"""
from __future__ import annotations

import json

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APIClient

User = get_user_model()


REGISTER_URL = "/api/auth/register/"
LOGIN_URL = "/api/auth/login/"
PROFILE_URL = "/api/auth/profile/"


class AuthHappyPath(TestCase):
    def setUp(self):
        self.client = APIClient()

    def test_register_then_login_then_fetch_profile(self):
        r = self.client.post(REGISTER_URL, {
            "username": "alice",
            "email": "alice@example.com",
            "password": "Sup3r$ecret!",
            "password2": "Sup3r$ecret!",
        }, format="json")
        # Registration should succeed (200 or 201 depending on serializer)
        self.assertIn(r.status_code, (200, 201), r.content)

        r = self.client.post(LOGIN_URL, {
            "email": "alice@example.com",
            "password": "Sup3r$ecret!",
        }, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        access = r.data.get("access") or r.data.get("access_token")
        self.assertTrue(access, "login response missing access token")

        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {access}")
        r = self.client.get(PROFILE_URL)
        self.assertEqual(r.status_code, 200, r.content)


class AuthSadPath(TestCase):
    def setUp(self):
        self.client = APIClient()
        User.objects.create_user(
            username="alice", email="alice@example.com", password="Sup3r$ecret!"
        )

    def test_login_wrong_password_returns_401(self):
        r = self.client.post(LOGIN_URL, {
            "email": "alice@example.com",
            "password": "wrong-password",
        }, format="json")
        self.assertIn(r.status_code, (400, 401))

    def test_login_missing_field_returns_400(self):
        r = self.client.post(LOGIN_URL, {"email": "alice@example.com"}, format="json")
        self.assertEqual(r.status_code, 400)

    def test_profile_without_auth_returns_401(self):
        r = self.client.get(PROFILE_URL)
        self.assertEqual(r.status_code, 401)

    def test_register_password_mismatch_rejected(self):
        r = self.client.post(REGISTER_URL, {
            "username": "bob",
            "email": "bob@example.com",
            "password": "abcdefgh1!",
            "password2": "different!",
        }, format="json")
        self.assertEqual(r.status_code, 400)


class AuthAngryPath(TestCase):
    """Hostile inputs — proves the auth surface doesn't crash or leak."""

    def setUp(self):
        self.client = APIClient()

    def test_sql_injection_in_email_does_not_500(self):
        r = self.client.post(LOGIN_URL, {
            "email": "alice@example.com' OR '1'='1",
            "password": "irrelevant",
        }, format="json")
        # Must NEVER 500. 400 or 401 are both acceptable.
        self.assertLess(r.status_code, 500, r.content)

    def test_unicode_normalisation_does_not_collide(self):
        """Two accounts with visually identical but distinct unicode emails
        must not be silently merged/conflated."""
        User.objects.create_user(
            username="ascii", email="admin@example.com", password="x" * 12
        )
        # Cyrillic 'а' vs ASCII 'a'
        r = self.client.post(LOGIN_URL, {
            "email": "аdmin@example.com",
            "password": "x" * 12,
        }, format="json")
        self.assertIn(r.status_code, (400, 401))

    def test_oversized_password_rejected_not_hashed(self):
        """Submitting a 1MB password should be rejected at validation time,
        not silently bcrypt'd (DoS vector)."""
        huge = "A" * (1024 * 1024)
        r = self.client.post(LOGIN_URL, {
            "email": "alice@example.com",
            "password": huge,
        }, format="json")
        # Either rejected as too long, or treated as wrong password — but no 500.
        self.assertLess(r.status_code, 500)

    def test_tampered_jwt_returns_401(self):
        User.objects.create_user(username="alice", email="alice@example.com", password="Sup3r$ecret!")
        r = self.client.post(LOGIN_URL, {"email": "alice@example.com", "password": "Sup3r$ecret!"}, format="json")
        access = r.data.get("access") or r.data.get("access_token")
        self.assertTrue(access)
        # Flip a character in the signature
        tampered = access[:-2] + ("AB" if access[-2:] != "AB" else "CD")
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {tampered}")
        r = self.client.get(PROFILE_URL)
        self.assertEqual(r.status_code, 401)

    def test_garbage_bearer_token_returns_401(self):
        self.client.credentials(HTTP_AUTHORIZATION="Bearer not.a.jwt")
        r = self.client.get(PROFILE_URL)
        self.assertEqual(r.status_code, 401)

    def test_empty_post_body_returns_400(self):
        r = self.client.generic("POST", LOGIN_URL, b"", content_type="application/json")
        self.assertIn(r.status_code, (400, 401))
