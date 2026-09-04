"""
What a stdio MCP server is allowed to see in its environment.

A stdio connector is third-party code we spawn. It used to inherit the whole of
`os.environ`, which in a deployed container is `Backend/.env` — so every curated
npm package was handed `SECRET_KEY`, `POSTGRES_PASSWORD`, and
`CREDENTIAL_ENCRYPTION_KEY`, the master key that decrypts *every* user's vault.
The credentials app resolves per-user and injects only the mapped fields
precisely so that key never leaves the process; one dict splat undid all of it.

These tests pin the replacement: the environment is built from an allowlist, not
inherited. The leak test is the point of the file — if it ever passes trivially
because the allowlist grew a wildcard, the rest is decoration.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase

from mcp_integration.client import (
    _ENV_PASSTHROUGH,
    _ENV_PASSTHROUGH_PREFIXES,
    _build_subprocess_env,
)


def _server(env=None):
    return SimpleNamespace(env=env or {})


def _resolved(env_vars=None):
    return SimpleNamespace(env_vars=env_vars or {})


# Names that must never reach a connector, whatever else changes.
FORBIDDEN = {
    "SECRET_KEY": "django-secret",
    "CREDENTIAL_ENCRYPTION_KEY": "the-master-vault-key",
    "POSTGRES_PASSWORD": "db-password",
    "DATABASE_URL": "postgres://user:pw@host/db",
    "GOOGLE_OAUTH_CLIENT_SECRET": "platform-oauth-secret",
    "AWS_SECRET_ACCESS_KEY": "aws-secret",
    "OPENROUTER_API_KEY": "sk-or-platform-key",
}


class SecretsDoNotReachConnectorsTests(SimpleTestCase):
    def test_platform_secrets_are_not_inherited(self):
        with patch.dict("os.environ", FORBIDDEN, clear=False):
            env = _build_subprocess_env(_server(), _resolved())
        for name in FORBIDDEN:
            self.assertNotIn(
                name, env,
                f"{name} reached a third-party MCP subprocess. The environment "
                f"must be built from _ENV_PASSTHROUGH, never inherited.",
            )

    def test_no_forbidden_value_leaks_under_a_different_name(self):
        """An allowlisted name must not carry a secret's *value* either."""
        with patch.dict("os.environ", FORBIDDEN, clear=False):
            env = _build_subprocess_env(_server(), _resolved())
        values = set(env.values())
        for name, value in FORBIDDEN.items():
            self.assertNotIn(value, values, f"value of {name} leaked into the env")

    def test_an_unlisted_name_is_dropped(self):
        with patch.dict("os.environ", {"SOME_FUTURE_SECRET": "nope"}, clear=False):
            env = _build_subprocess_env(_server(), _resolved())
        self.assertNotIn("SOME_FUTURE_SECRET", env)


class LaunchersStillWorkTests(SimpleTestCase):
    """
    The allowlist is only correct if `npx` can still run. These are the names
    without which a launcher cannot resolve a package or find its cache.
    """

    def test_path_is_passed_through(self):
        with patch.dict("os.environ", {"PATH": "/usr/bin"}, clear=False):
            env = _build_subprocess_env(_server(), _resolved())
        self.assertEqual(env.get("PATH"), "/usr/bin")

    def test_npm_and_node_prefixes_are_passed_through(self):
        extra = {"NODE_EXTRA_CA_CERTS": "/ca.pem", "NPM_CONFIG_REGISTRY": "https://r"}
        with patch.dict("os.environ", extra, clear=False):
            env = _build_subprocess_env(_server(), _resolved())
        for key, value in extra.items():
            self.assertEqual(env.get(key), value)

    def test_matching_is_case_insensitive(self):
        """
        Windows spells them `Path`/`Temp`, POSIX `PATH`/`TEMP`, and matching on
        the exact spelling would drop PATH on one of the two platforms — which
        is a connector that cannot find `npx` at all.

        Asserted on the *value*, because Windows' `os.environ` upper-cases keys
        on write while POSIX preserves them: pinning the returned spelling would
        make this test pass on one platform and fail on the other.
        """
        with patch.dict("os.environ", {"Path": "C:/bin", "Temp": "C:/tmp"}, clear=False):
            env = _build_subprocess_env(_server(), _resolved())
        folded = {k.upper(): v for k, v in env.items()}
        self.assertEqual(folded.get("PATH"), "C:/bin")
        self.assertEqual(folded.get("TEMP"), "C:/tmp")

    def test_the_docker_npm_cache_survives(self):
        """
        The image sets `NPM_CONFIG_CACHE=/opt/npm-cache` and pre-warms every
        curated package into it, because cold `npx -y` is ~21 s against a 25 s
        CONNECT_TIMEOUT. Filtering this name out would not break a test — it
        would make every connector in production a coin flip on first use.
        """
        with patch.dict("os.environ", {"NPM_CONFIG_CACHE": "/opt/npm-cache"}, clear=False):
            env = _build_subprocess_env(_server(), _resolved())
        folded = {k.upper(): v for k, v in env.items()}
        self.assertEqual(folded.get("NPM_CONFIG_CACHE"), "/opt/npm-cache")

    def test_allowlist_has_no_wildcard(self):
        """
        A prefix of "" would pass everything through and make the leak test
        pass for the wrong reason.
        """
        self.assertNotIn("", _ENV_PASSTHROUGH)
        self.assertTrue(all(p for p in _ENV_PASSTHROUGH_PREFIXES))


class PrecedenceTests(SimpleTestCase):
    def test_server_env_overrides_passthrough(self):
        with patch.dict("os.environ", {"LANG": "C"}, clear=False):
            env = _build_subprocess_env(_server({"LANG": "en_US.UTF-8"}), _resolved())
        self.assertEqual(env["LANG"], "en_US.UTF-8")

    def test_injected_credentials_override_everything(self):
        """
        The injector's per-user value is the whole point; an operator's
        `server.env` must not be able to pin a different user's token.
        """
        with patch.dict("os.environ", {"TOKEN": "from-host"}, clear=False):
            env = _build_subprocess_env(
                _server({"TOKEN": "from-server-env"}),
                _resolved({"TOKEN": "from-user-vault"}),
            )
        self.assertEqual(env["TOKEN"], "from-user-vault")

    def test_injected_credentials_are_always_present(self):
        """A mapped credential is never filtered by the allowlist."""
        env = _build_subprocess_env(_server(), _resolved({"NOTION_TOKEN": "secret_x"}))
        self.assertEqual(env["NOTION_TOKEN"], "secret_x")
