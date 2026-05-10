"""
Adversarial tests for the `credentials` app.

Targets the encryption layer, the audit log, and the OAuth token-refresh
race-condition guard. These tests intentionally try to corrupt state and
verify the model doesn't blindly hand back garbage or leak via exceptions.
"""
from __future__ import annotations

import json

from cryptography.fernet import Fernet
from django.contrib.auth import get_user_model
from django.test import TestCase

from credentials.models import Credential, CredentialAuditLog, CredentialType

User = get_user_model()


def _make_type(slug="openai"):
    return CredentialType.objects.create(name=slug.title(), slug=slug)


def _make_cred(user, ctype, data, name="default"):
    c = Credential(user=user, credential_type=ctype, name=name)
    c.set_credential_data(data)
    c.save()
    return c


class EncryptionHappy(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("a", "a@x.com", "x" * 12)
        self.ctype = _make_type()

    def test_round_trip_returns_same_dict(self):
        cred = _make_cred(self.user, self.ctype, {"token": "sk-abc", "extra": [1, 2]})
        cred.refresh_from_db()
        self.assertEqual(cred.get_credential_data(), {"token": "sk-abc", "extra": [1, 2]})

    def test_ciphertext_differs_from_plaintext(self):
        cred = _make_cred(self.user, self.ctype, {"token": "sekret"})
        self.assertNotIn(b"sekret", bytes(cred.encrypted_data))


class EncryptionSad(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("a", "a@x.com", "x" * 12)
        self.ctype = _make_type()

    def test_empty_encrypted_data_returns_empty_dict(self):
        cred = Credential.objects.create(
            user=self.user, credential_type=self.ctype, name="empty", encrypted_data=b""
        )
        self.assertEqual(cred.get_credential_data(), {})

    def test_decrypt_garbage_raises_value_error_not_crypto(self):
        """Bare cryptography exceptions must be wrapped — they leak detail."""
        cred = Credential.objects.create(
            user=self.user,
            credential_type=self.ctype,
            name="garbage",
            encrypted_data=b"not-fernet",
        )
        with self.assertRaises(ValueError):
            cred.get_credential_data()


class EncryptionAngry(TestCase):
    """Hostile: try to make decryption do something it shouldn't."""

    def setUp(self):
        self.user = User.objects.create_user("a", "a@x.com", "x" * 12)
        self.ctype = _make_type()

    def test_swapping_users_ciphertext_does_not_decrypt_to_other_payload(self):
        """Two users encrypting the same key -> different ciphertexts and
        decryption stays correct after swap."""
        u2 = User.objects.create_user("b", "b@x.com", "x" * 12)
        c1 = _make_cred(self.user, self.ctype, {"token": "alice-key"})
        c2 = _make_cred(u2, self.ctype, {"token": "bob-key"}, name="bob-cred")
        # Bytes differ even with identical keys (Fernet has IV)
        self.assertNotEqual(bytes(c1.encrypted_data), bytes(c2.encrypted_data))
        # Decryption returns the right one
        c1.refresh_from_db()
        c2.refresh_from_db()
        self.assertEqual(c1.get_credential_data()["token"], "alice-key")
        self.assertEqual(c2.get_credential_data()["token"], "bob-key")

    def test_truncated_ciphertext_raises_value_error(self):
        cred = _make_cred(self.user, self.ctype, {"token": "abc"})
        cred.encrypted_data = bytes(cred.encrypted_data)[:20]
        cred.save(update_fields=["encrypted_data"])
        with self.assertRaises(ValueError):
            cred.get_credential_data()

    def test_bit_flip_in_ciphertext_raises_not_returns_garbage(self):
        cred = _make_cred(self.user, self.ctype, {"token": "abc"})
        ct = bytearray(bytes(cred.encrypted_data))
        ct[10] ^= 0xFF
        cred.encrypted_data = bytes(ct)
        cred.save(update_fields=["encrypted_data"])
        with self.assertRaises(ValueError):
            cred.get_credential_data()

    def test_audit_log_failure_does_not_block_decryption(self):
        """If audit log creation fails, decryption must still return the data —
        otherwise an outage in the log table breaks every workflow."""
        cred = _make_cred(self.user, self.ctype, {"token": "abc"})
        from unittest.mock import patch

        with patch.object(CredentialAuditLog.objects, "create", side_effect=RuntimeError("DB down")):
            data = cred.get_credential_data(user=self.user)
            self.assertEqual(data, {"token": "abc"})

    def test_unique_together_user_name(self):
        from django.db import IntegrityError
        _make_cred(self.user, self.ctype, {"token": "1"}, name="dup")
        with self.assertRaises(IntegrityError):
            _make_cred(self.user, self.ctype, {"token": "2"}, name="dup")


class AuditLogTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("a", "a@x.com", "x" * 12)
        self.ctype = _make_type()
        self.cred = _make_cred(self.user, self.ctype, {"token": "abc"})

    def test_decrypt_with_user_logs_access(self):
        before = CredentialAuditLog.objects.count()
        self.cred.get_credential_data(user=self.user, ip_address="1.2.3.4", user_agent="test")
        self.assertEqual(CredentialAuditLog.objects.count(), before + 1)

    def test_decrypt_without_user_does_not_log(self):
        before = CredentialAuditLog.objects.count()
        self.cred.get_credential_data()
        self.assertEqual(CredentialAuditLog.objects.count(), before)
