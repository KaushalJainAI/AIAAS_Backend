"""
Credential files: rendering them, and getting rid of them.

Some MCP servers read credentials from disk rather than from the environment —
`@isaacphi/mcp-gdrive` and `@cocal/google-calendar-mcp` both do, which is the
only reason Drive, Sheets and Calendar could not be enabled alongside Gmail.

The rendering half is ordinary template substitution. The half that needs
testing is what ends up on the filesystem: these files contain a plaintext
refresh token, so "is it removed" and "can another user read it" are the
questions, and neither is answered by a connector merely working.
"""
from __future__ import annotations

import asyncio
import os
from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase, override_settings

from mcp_integration.client import _remove_credential_dir, _write_credential_files
from mcp_integration.credential_injector import (
    CredentialFile,
    CredentialInjector,
    CredentialInvalidError,
    ResolvedCredentials,
)


def _run(coro):
    return asyncio.run(coro)


def _server(file_map=None, name="srv"):
    return SimpleNamespace(
        id=7,
        name=name,
        env={},
        required_credential_types=[],
        credential_env_map={},
        credential_header_map={},
        credential_file_map=file_map or {},
    )


def _stub(data):
    """Stand in for the credentials manager, as in test_units."""
    from credentials.manager import CredentialManager

    async def _get_credential(self, credential_id, user_id, refresh_if_expired=True):
        return data

    # `lookup_by_slug_sync` is a staticmethod; patching it with a bare lambda
    # would rebind it as an instance method and hand it `self` as the slug.
    return patch.multiple(
        CredentialManager,
        lookup_by_slug_sync=staticmethod(lambda slug, user_id: SimpleNamespace(id=1)),
        get_credential=_get_credential,
    )


class RenderingTests(SimpleTestCase):
    def test_a_placeholder_in_a_nested_string_is_filled(self):
        server = _server({
            "TOKEN_PATH": {
                "filename": "tokens.json",
                "content": {"normal": {"refresh_token": "{google-oauth2:refresh_token}"}},
            },
        })
        with _stub({"refresh_token": "1//rt"}):
            resolved = _run(CredentialInjector.resolve(server, 1))
        self.assertEqual(len(resolved.files), 1)
        self.assertIn("1//rt", resolved.files[0].content)

    @override_settings(GOOGLE_OAUTH_CLIENT_ID="platform-id")
    def test_the_settings_sentinel_works_inside_a_file(self):
        """
        The platform's OAuth client belongs in the keys file, and it is ours,
        not the user's — the same reason `@settings:` exists for env vars.
        """
        server = _server({
            "KEYS": {
                "filename": "keys.json",
                "content": {"installed": {"client_id": "{@settings:GOOGLE_OAUTH_CLIENT_ID}"}},
            },
        })
        with _stub({}):
            resolved = _run(CredentialInjector.resolve(server, 1))
        self.assertIn("platform-id", resolved.files[0].content)

    def test_a_missing_field_is_an_error_not_a_silently_empty_file(self):
        server = _server({
            "TOKEN_PATH": {
                "filename": "tokens.json",
                "content": {"refresh_token": "{google-oauth2:refresh_token}"},
            },
        })
        with _stub({"something_else": "x"}):
            with self.assertRaises(CredentialInvalidError):
                _run(CredentialInjector.resolve(server, 1))

    def test_a_filename_cannot_escape_its_directory(self):
        """
        The map is editable on a user's own server, so the filename is
        untrusted input joined onto a path we created.
        """
        for bad in ["../escape.json", "sub/dir.json", "..", "", None]:
            with self.subTest(filename=bad):
                server = _server({"X": {"filename": bad, "content": {"a": "b"}}})
                with _stub({}):
                    with self.assertRaises(CredentialInvalidError):
                        _run(CredentialInjector.resolve(server, 1))

    def test_content_is_required(self):
        server = _server({"X": {"filename": "a.json"}})
        with _stub({}):
            with self.assertRaises(CredentialInvalidError):
                _run(CredentialInjector.resolve(server, 1))

    def test_resolve_writes_nothing_to_disk(self):
        """
        `validate()` calls `resolve()` on every Connections page load. If
        resolving materialised files, viewing the page would scatter refresh
        tokens across the filesystem with nothing responsible for removing them.
        """
        server = _server({
            "TOKEN_PATH": {
                "filename": "t.json",
                "content": {"r": "{google-oauth2:refresh_token}"},
            },
        })
        with _stub({"refresh_token": "1//rt"}):
            resolved = _run(CredentialInjector.resolve(server, 1))
        self.assertIsInstance(resolved.files[0], CredentialFile)
        self.assertFalse(os.path.exists(resolved.files[0].filename))


class MaterialisationTests(SimpleTestCase):
    def _resolved(self, *files):
        return ResolvedCredentials(
            env_vars={}, headers={}, used_credential_ids=[], files=list(files)
        )

    def test_no_files_means_no_directory(self):
        directory, env = _write_credential_files(_server(), 1, self._resolved())
        self.assertIsNone(directory)
        self.assertEqual(env, {})

    def test_target_file_points_at_the_file_and_dir_at_the_folder(self):
        resolved = self._resolved(
            CredentialFile("KEYS", "keys.json", "{}", "file"),
            CredentialFile("CREDS_DIR", ".creds.json", "{}", "dir"),
        )
        directory, env = _write_credential_files(_server(), 1, resolved)
        try:
            self.assertEqual(env["KEYS"], os.path.join(directory, "keys.json"))
            self.assertEqual(env["CREDS_DIR"], directory)
            self.assertTrue(os.path.exists(os.path.join(directory, ".creds.json")))
        finally:
            _remove_credential_dir(directory)

    def test_two_users_never_share_a_directory(self):
        """
        The per-user property. The session pool is keyed on
        (server_id, user_id), so one worker exists per pair; each must get its
        own directory or one account's refresh token sits where another
        account's subprocess can read it.
        """
        a = self._resolved(CredentialFile("D", "t.json", "user-a-token", "dir"))
        b = self._resolved(CredentialFile("D", "t.json", "user-b-token", "dir"))
        dir_a, _ = _write_credential_files(_server(), 1, a)
        dir_b, _ = _write_credential_files(_server(), 2, b)
        try:
            self.assertNotEqual(dir_a, dir_b)
            with open(os.path.join(dir_a, "t.json")) as fh:
                self.assertEqual(fh.read(), "user-a-token")
            with open(os.path.join(dir_b, "t.json")) as fh:
                self.assertEqual(fh.read(), "user-b-token")
        finally:
            _remove_credential_dir(dir_a)
            _remove_credential_dir(dir_b)

    def test_the_directory_is_removed(self):
        resolved = self._resolved(CredentialFile("D", "t.json", "secret", "dir"))
        directory, _ = _write_credential_files(_server(), 1, resolved)
        self.assertTrue(os.path.isdir(directory))
        _remove_credential_dir(directory)
        self.assertFalse(os.path.exists(directory))

    def test_a_failed_write_leaves_nothing_behind(self):
        """A half-written directory is still a directory with a token in it."""
        good = CredentialFile("A", "a.json", "secret", "file")
        bad = CredentialFile("B", "b.json", None, "file")  # None -> write raises
        created = []
        real_mkdtemp = __import__("tempfile").mkdtemp

        def _spy(*args, **kwargs):
            path = real_mkdtemp(*args, **kwargs)
            created.append(path)
            return path

        with patch("mcp_integration.client.tempfile.mkdtemp", _spy):
            with self.assertRaises(Exception):
                _write_credential_files(_server(), 1, self._resolved(good, bad))

        self.assertEqual(len(created), 1)
        self.assertFalse(
            os.path.exists(created[0]),
            "A failed materialisation must remove its own directory.",
        )

    def test_removal_of_a_missing_directory_is_not_an_error(self):
        """Cleanup runs on every unwind, including ones where nothing was made."""
        _remove_credential_dir(None)
        _remove_credential_dir(os.path.join(os.sep, "nope", "does-not-exist"))
