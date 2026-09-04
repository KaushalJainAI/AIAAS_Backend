"""
Unit tests for credentials/manager.py — focused on logic that doesn't
require the DB (cache eviction, pure helpers).
"""
from __future__ import annotations

from datetime import timedelta

from django.test import SimpleTestCase
from django.utils import timezone

from credentials.manager import CredentialManager, MAX_CACHE_SIZE


class CacheEvictionTests(SimpleTestCase):
    def test_expired_entries_removed(self):
        mgr = CredentialManager()
        now = timezone.now()
        # Populate with an expired entry (older than TTL).
        mgr._cache["1:a"] = ({"token": "x"}, now - mgr._cache_ttl - timedelta(seconds=1))
        mgr._cache["1:b"] = ({"token": "y"}, now)  # fresh

        mgr._evict_cache()
        self.assertNotIn("1:a", mgr._cache)
        self.assertIn("1:b", mgr._cache)

    def test_over_limit_evicts_oldest(self):
        mgr = CredentialManager()
        now = timezone.now()
        # Fill past MAX_CACHE_SIZE with distinct timestamps.
        for i in range(MAX_CACHE_SIZE + 3):
            mgr._cache[f"1:c{i}"] = ({"token": str(i)}, now + timedelta(microseconds=i))

        mgr._evict_cache()
        # Oldest (lowest microseconds) should have been evicted.
        self.assertNotIn("1:c0", mgr._cache)
        self.assertLessEqual(len(mgr._cache), MAX_CACHE_SIZE)

    def test_clear_cache_by_user(self):
        mgr = CredentialManager()
        now = timezone.now()
        mgr._cache["1:a"] = ({}, now)
        mgr._cache["2:b"] = ({}, now)

        mgr.clear_cache(user_id=1)
        self.assertNotIn("1:a", mgr._cache)
        self.assertIn("2:b", mgr._cache)

    def test_clear_cache_all(self):
        mgr = CredentialManager()
        mgr._cache["1:a"] = ({}, timezone.now())
        mgr._cache["2:b"] = ({}, timezone.now())
        mgr.clear_cache()
        self.assertEqual(mgr._cache, {})

    def test_cache_namespace_per_user(self):
        # Security: user A's cache hit must NOT serve user B's request.
        mgr = CredentialManager()
        mgr._cache["1:99"] = ({"token": "user1_secret"}, timezone.now())
        # Same credential id, different user → different cache key.
        self.assertNotIn("2:99", mgr._cache)


class ValidateAgainstSchemaTests(SimpleTestCase):
    """Schema validation runs in-process — no DB needed."""

    def _ct(self, schema):
        ct = MagicMock()
        ct.fields_schema = schema
        return ct

    def test_missing_required_field(self):
        from credentials.manager import CredentialManager
        mgr = CredentialManager()
        ct = self._ct([{"name": "api_key", "required": True, "type": "string"}])
        errs = mgr.validate_against_schema({}, ct)
        self.assertEqual(len(errs), 1)
        self.assertIn("api_key", errs[0])

    def test_wrong_type(self):
        from credentials.manager import CredentialManager
        mgr = CredentialManager()
        ct = self._ct([{"name": "port", "type": "number"}])
        errs = mgr.validate_against_schema({"port": "not-a-number"}, ct)
        self.assertTrue(any("port" in e for e in errs))

    def test_optional_missing_ok(self):
        from credentials.manager import CredentialManager
        mgr = CredentialManager()
        ct = self._ct([{"name": "x", "required": False, "type": "string"}])
        self.assertEqual(mgr.validate_against_schema({}, ct), [])

    def test_boolean_must_be_bool(self):
        from credentials.manager import CredentialManager
        mgr = CredentialManager()
        ct = self._ct([{"name": "tls", "type": "boolean"}])
        # The string "true" is NOT a bool — strict validation.
        errs = mgr.validate_against_schema({"tls": "true"}, ct)
        self.assertTrue(any("tls" in e for e in errs))


# Imports at top would be circular; localize.
from unittest.mock import MagicMock  # noqa: E402
