"""
Unit tests for mcp_integration — credential injector and tool cache.

Security focus:
- Credential placeholders must NOT leak across servers
- Missing credentials must raise typed errors (not silently inject "")
- Field extraction must reject path traversal-style inputs
- _coerce_user_id must accept both int and User-like inputs without crashing
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from django.test import SimpleTestCase

from mcp_integration.credential_injector import (
    CredentialInjector, CredentialInvalidError, CredentialMissingError,
    ResolvedCredentials, _coerce_user_id, _extract_field,
    _MAPPING_RE, _PLACEHOLDER_RE,
)


def _run(coro):
    return asyncio.run(coro)


# ─────────────────────────────────────────────────────────────────────────
# Pure helpers
# ─────────────────────────────────────────────────────────────────────────

class CoerceUserIdTests(SimpleTestCase):
    def test_none_returns_none(self):
        self.assertIsNone(_coerce_user_id(None))

    def test_int_passthrough(self):
        self.assertEqual(_coerce_user_id(42), 42)

    def test_user_like_object(self):
        user = SimpleNamespace(id=7)
        self.assertEqual(_coerce_user_id(user), 7)

    def test_object_without_id_returns_none(self):
        self.assertIsNone(_coerce_user_id(SimpleNamespace()))


class ExtractFieldTests(SimpleTestCase):
    def test_top_level(self):
        self.assertEqual(_extract_field({"k": "v"}, "k"), "v")

    def test_nested_dot_path(self):
        self.assertEqual(
            _extract_field({"a": {"b": {"c": 1}}}, "a.b.c"), 1
        )

    def test_missing_returns_none(self):
        self.assertIsNone(_extract_field({"a": 1}, "missing"))
        self.assertIsNone(_extract_field({"a": {"b": 1}}, "a.c"))

    def test_traversal_through_non_dict_returns_none(self):
        # If a path part lands on a string/int, we must not crash and must not
        # leak via Python attribute access (e.g. "real.upper").
        self.assertIsNone(_extract_field({"a": "string"}, "a.upper"))
        self.assertIsNone(_extract_field({"a": [1, 2]}, "a.0"))


class RegexShapeTests(SimpleTestCase):
    def test_mapping_re_full_match(self):
        m = _MAPPING_RE.match("github:token")
        self.assertIsNotNone(m)
        self.assertEqual((m.group(1), m.group(2)), ("github", "token"))

    def test_mapping_re_rejects_no_colon(self):
        self.assertIsNone(_MAPPING_RE.match("notacolon"))

    def test_mapping_re_allows_dotted_field(self):
        m = _MAPPING_RE.match("aws:credentials.access_key")
        self.assertIsNotNone(m)
        self.assertEqual(m.group(2), "credentials.access_key")

    def test_placeholder_re_finds_multiple(self):
        text = "Bearer {gh:token} for {aws:key}"
        matches = list(_PLACEHOLDER_RE.finditer(text))
        self.assertEqual(len(matches), 2)


# ─────────────────────────────────────────────────────────────────────────
# CredentialInjector.resolve  — uses patched _resolve_slug to avoid DB
# ─────────────────────────────────────────────────────────────────────────

def _fake_cred(cid: int, data: dict, name: str = "test"):
    cred = MagicMock()
    cred.id = cid
    cred.name = name
    cred.get_credential_data = MagicMock(return_value=data)
    return cred


def _make_server(*, required=None, env_map=None, header_map=None, name="srv"):
    s = MagicMock()
    s.name = name
    s.required_credential_types = required or []
    s.credential_env_map = env_map or {}
    s.credential_header_map = header_map or {}
    return s


class CredentialInjectorResolveTests(SimpleTestCase):
    def test_no_user_returns_empty(self):
        server = _make_server(required=["github"])
        result = _run(CredentialInjector.resolve(server, None))
        self.assertEqual(result.env_vars, {})
        self.assertEqual(result.headers, {})

    def test_missing_required_raises(self):
        server = _make_server(required=["github"])
        with patch(
            "mcp_integration.credential_injector._lookup_credential_sync",
            return_value=None,
        ):
            with self.assertRaises(CredentialMissingError):
                _run(CredentialInjector.resolve(server, 1))

    def test_env_map_resolves_field(self):
        server = _make_server(env_map={"GITHUB_TOKEN": "github:token"})
        cred = _fake_cred(99, {"token": "ghp_secret"})
        with patch(
            "mcp_integration.credential_injector._lookup_credential_sync",
            return_value=cred,
        ):
            result = _run(CredentialInjector.resolve(server, 1))
        self.assertEqual(result.env_vars, {"GITHUB_TOKEN": "ghp_secret"})
        self.assertIn(99, result.used_credential_ids)

    def test_header_map_substitutes_placeholder(self):
        server = _make_server(
            header_map={"Authorization": "Bearer {github:token}"}
        )
        cred = _fake_cred(1, {"token": "abc"})
        with patch(
            "mcp_integration.credential_injector._lookup_credential_sync",
            return_value=cred,
        ):
            result = _run(CredentialInjector.resolve(server, 1))
        self.assertEqual(result.headers["Authorization"], "Bearer abc")

    def test_invalid_field_raises_invalid_error(self):
        # Map references a field that doesn't exist on the credential.
        server = _make_server(env_map={"X": "github:does_not_exist"})
        cred = _fake_cred(1, {"token": "x"})
        with patch(
            "mcp_integration.credential_injector._lookup_credential_sync",
            return_value=cred,
        ):
            with self.assertRaises(CredentialInvalidError):
                _run(CredentialInjector.resolve(server, 1))

    def test_skips_invalid_mapping_strings(self):
        # Non-matching shape (no colon) must be skipped without crashing.
        server = _make_server(env_map={"X": "no-colon-here"})
        with patch(
            "mcp_integration.credential_injector._lookup_credential_sync",
            return_value=_fake_cred(1, {"token": "x"}),
        ):
            result = _run(CredentialInjector.resolve(server, 1))
        # Bad mapping skipped → empty env_vars but no exception.
        self.assertEqual(result.env_vars, {})

    def test_static_header_passthrough(self):
        # Header value with no placeholders is used verbatim.
        server = _make_server(header_map={"X-Static": "literal-value"})
        result = _run(CredentialInjector.resolve(server, 1))
        self.assertEqual(result.headers["X-Static"], "literal-value")

    def test_credential_cache_avoids_duplicate_lookups(self):
        # Same slug used twice should hit DB once.
        server = _make_server(env_map={
            "A": "github:token",
            "B": "github:token",
        })
        cred = _fake_cred(1, {"token": "shared"})
        with patch(
            "mcp_integration.credential_injector._lookup_credential_sync",
            return_value=cred,
        ) as look:
            _run(CredentialInjector.resolve(server, 1))
        self.assertEqual(look.call_count, 1)


class CredentialInjectorValidateTests(SimpleTestCase):
    def test_returns_empty_on_success(self):
        server = _make_server(env_map={"X": "github:token"})
        with patch(
            "mcp_integration.credential_injector._lookup_credential_sync",
            return_value=_fake_cred(1, {"token": "ok"}),
        ):
            errs = _run(CredentialInjector.validate(server, 1))
        self.assertEqual(errs, [])

    def test_returns_message_on_missing(self):
        server = _make_server(required=["github"])
        with patch(
            "mcp_integration.credential_injector._lookup_credential_sync",
            return_value=None,
        ):
            errs = _run(CredentialInjector.validate(server, 1))
        self.assertEqual(len(errs), 1)
        self.assertIn("github", errs[0])
