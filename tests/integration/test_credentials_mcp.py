"""
Cross-app integration: credentials → MCP credential injection.

This is the highest-risk surface in the platform — a leak here means another
user's secrets get loaded into an MCP subprocess. Tests guard against that.
"""
from __future__ import annotations

from asgiref.sync import async_to_sync
from django.contrib.auth import get_user_model
from django.test import TestCase

from credentials.models import Credential, CredentialType
from mcp_integration.credential_injector import (
    CredentialInjector,
    CredentialInvalidError,
    CredentialMissingError,
)
from mcp_integration.models import MCPServer

User = get_user_model()


def _make_cred_type(slug="github"):
    # update_or_create, not create: `credentials.0005` seeds the real catalog when
    # the test database migrates, so this slug may already exist. Overwriting pins
    # the schema these tests assert against instead of inheriting the seeder's.
    ctype, _ = CredentialType.objects.update_or_create(
        slug=slug,
        defaults={
            "name": slug.title(),
            "fields_schema": [{"name": "token", "type": "password", "required": True}],
        },
    )
    return ctype


def _make_cred(user, ctype, *, name="default", data=None):
    cred = Credential(user=user, credential_type=ctype, name=name)
    cred.set_credential_data(data or {"token": "tok-" + user.username})
    cred.save()
    return cred


def _make_server(*, env_map=None, header_map=None, required=None):
    return MCPServer.objects.create(
        name="github-srv",
        type="stdio",
        command="echo",
        args=["hi"],
        env={},
        required_credential_types=required or ["github"],
        credential_env_map=env_map or {"GITHUB_TOKEN": "github:token"},
        credential_header_map=header_map or {},
        enabled=True,
        user=None,
    )


class _CacheIsolated(TestCase):
    """Base for every case here that resolves a credential.

    `CredentialManager` is a process-global singleton with a 5-minute cache of
    decrypted data, keyed `{user_id}:{credential_id}`, and the injector reads
    through it. Test databases restart ids at 1, so those keys collide across
    cases and one test reads the previous test's plaintext — which is why
    `CredentialInjectionHappy` passed alone and failed in a full run.

    That is a correctness problem as well as a flake: `CredentialInjectionAngry`
    exists to catch one user reading another's secret, and a stale cache entry
    is precisely the shape of bug it would mask.
    """

    def setUp(self):
        super().setUp()
        from credentials.manager import get_credential_manager
        get_credential_manager().clear_cache()


class CredentialInjectionHappy(_CacheIsolated):
    def setUp(self):
        super().setUp()
        self.alice = User.objects.create_user("alice", "a@x.com", "x" * 12)
        self.ctype = _make_cred_type()
        _make_cred(self.alice, self.ctype, data={"token": "alice-token"})
        self.server = _make_server()

    def test_resolves_env_var_for_owner(self):
        resolved = async_to_sync(CredentialInjector.resolve)(self.server, self.alice)
        self.assertEqual(resolved.env_vars, {"GITHUB_TOKEN": "alice-token"})


class CredentialInjectionSad(_CacheIsolated):
    def setUp(self):
        super().setUp()
        self.alice = User.objects.create_user("alice", "a@x.com", "x" * 12)
        self.ctype = _make_cred_type()
        self.server = _make_server()

    def test_missing_credential_raises(self):
        with self.assertRaises(CredentialMissingError):
            async_to_sync(CredentialInjector.resolve)(self.server, self.alice)

    def test_credential_present_but_field_missing_raises_invalid(self):
        _make_cred(self.alice, self.ctype, data={"not_token": "x"})
        with self.assertRaises(CredentialInvalidError):
            async_to_sync(CredentialInjector.resolve)(self.server, self.alice)

    def test_no_user_returns_empty_resolution(self):
        resolved = async_to_sync(CredentialInjector.resolve)(self.server, None)
        self.assertEqual(resolved.env_vars, {})
        self.assertEqual(resolved.headers, {})


class CredentialInjectionAngry(_CacheIsolated):
    """Adversarial — these are the tests that catch privilege-escalation bugs."""

    def setUp(self):
        super().setUp()
        self.alice = User.objects.create_user("alice", "a@x.com", "x" * 12)
        self.mallory = User.objects.create_user("mallory", "m@x.com", "x" * 12)
        self.ctype = _make_cred_type()
        _make_cred(self.alice, self.ctype, data={"token": "alice-secret"})
        self.server = _make_server()

    def test_other_user_cannot_borrow_alices_credential(self):
        """If Mallory has no github credential, resolution must fail —
        NOT silently grab Alice's."""
        with self.assertRaises(CredentialMissingError):
            async_to_sync(CredentialInjector.resolve)(self.server, self.mallory)

    def test_inactive_credential_treated_as_missing(self):
        Credential.objects.filter(user=self.alice).update(is_active=False)
        with self.assertRaises(CredentialMissingError):
            async_to_sync(CredentialInjector.resolve)(self.server, self.alice)

    def test_mapping_with_no_colon_is_skipped_not_crashed(self):
        self.server.credential_env_map = {"BAD": "no-colon-here"}
        self.server.save()
        # Should not raise — invalid mappings are logged and skipped.
        resolved = async_to_sync(CredentialInjector.resolve)(self.server, self.alice)
        self.assertNotIn("BAD", resolved.env_vars)

    def test_mapping_to_nonexistent_slug_raises_missing(self):
        self.server.required_credential_types = ["github"]
        self.server.credential_env_map = {"X": "nonexistent-slug:token"}
        self.server.save()
        with self.assertRaises(CredentialMissingError):
            async_to_sync(CredentialInjector.resolve)(self.server, self.alice)

    def test_path_traversal_field_returns_none_field(self):
        """`token..` style paths must not traverse into magic attrs."""
        self.server.credential_env_map = {"X": "github:token..__class__"}
        self.server.save()
        with self.assertRaises(CredentialInvalidError):
            async_to_sync(CredentialInjector.resolve)(self.server, self.alice)

    def test_decryption_with_wrong_key_raises_invalid(self):
        """If a credential row's encrypted_data is corrupt, surfaces as
        CredentialInvalidError — never as a bare crypto exception."""
        cred = Credential.objects.get(user=self.alice)
        cred.encrypted_data = b"definitely-not-fernet-ciphertext"
        cred.save(update_fields=["encrypted_data"])
        with self.assertRaises(CredentialInvalidError):
            async_to_sync(CredentialInjector.resolve)(self.server, self.alice)
