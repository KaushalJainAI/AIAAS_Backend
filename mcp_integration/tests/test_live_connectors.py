"""
Start every enabled curated connector for real and read back its tool list.

Skipped unless `MCP_LIVE_TESTS=1`, because it spawns npx subprocesses and needs
the network. Run it before shipping a catalogue change:

    MCP_LIVE_TESTS=1 python manage.py test mcp_integration.tests.test_live_connectors

Why this exists
---------------
`CuratedPackageTests` is offline by design: it checks a row's package spec
against a hand-maintained `VERIFIED` dict. That catches a repoint nobody wrote
down, and nothing else. It cannot tell whether the package still starts, whether
its tool list changed, or whether a credential mapping names a variable the
server does not read.

The catalogue has now been wrong in three distinct ways that only a running
process could reveal:

  * six specs that were never published (`0005`, caught months later);
  * a mapping short one variable — Slack exits at startup without
    `SLACK_TEAM_ID` — and one naming the wrong variable entirely, Notion's
    `NOTION_API_KEY` against a server that reads `NOTION_TOKEN`;
  * five rows disabled on a measurement that could not have been right:
    "it blocks for ever" was observed by running the binary bare, and a stdio
    MCP server *always* blocks when run bare — it is waiting for JSON-RPC on
    stdin. Four of those five worked (`0012`, `0014`).

So the honest test of "does this connector work" is the handshake, and it has to
be written down somewhere a person can run it.

Credentials are stand-ins. A server validates its token on first *use*, not at
startup, so `initialize` + `tools/list` succeeds with a fake refresh token —
which is exactly what makes this runnable in CI without anyone's real account.
It therefore proves the row starts and advertises what we claim, not that a
token works.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import unittest

from django.test import TestCase, override_settings

from mcp_integration.client import (
    _build_subprocess_env,
    _remove_credential_dir,
    _write_credential_files,
)
from mcp_integration.credential_injector import CredentialFile, ResolvedCredentials
from mcp_integration.models import MCPServer
from mcp_integration.tests.test_fresh_install import CuratedPackageTests

LIVE = os.environ.get("MCP_LIVE_TESTS") == "1"

#: Stand-in values for every placeholder the curated catalogue can reference.
#: A new mapping that names something absent here fails loudly rather than
#: rendering the string "None" into a credentials file.
STAND_INS = {
    "google-oauth2:refresh_token": "1//stand-in-refresh-token",
    "google-oauth2:access_token": "ya29.stand-in-access-token",
    "notion:token": "secret_stand-in",
    "slack:token": "xoxb-stand-in",
    "slack:teamId": "T0000000000",
    "@settings:GOOGLE_OAUTH_CLIENT_ID": "stand-in.apps.googleusercontent.com",
    "@settings:GOOGLE_OAUTH_CLIENT_SECRET": "stand-in-secret",
}

_PLACEHOLDER = re.compile(r"\{(@?[a-zA-Z0-9_\-]+):([a-zA-Z0-9_\-.]+)\}")

# Generous: a cold `npx -y` resolves and installs before the server prints a
# byte. This is a deliberate check, not a request budget.
HANDSHAKE_TIMEOUT = 180.0


def _stand_in(key: str) -> str:
    if key not in STAND_INS:
        raise AssertionError(
            f"The catalogue references {key!r} but this test has no stand-in "
            f"for it. Add one to STAND_INS."
        )
    return STAND_INS[key]


def _render(node):
    if isinstance(node, str):
        return _PLACEHOLDER.sub(
            lambda m: _stand_in(f"{m.group(1)}:{m.group(2)}"), node
        )
    if isinstance(node, dict):
        return {k: _render(v) for k, v in node.items()}
    if isinstance(node, list):
        return [_render(v) for v in node]
    return node


def _resolved_for(server: MCPServer) -> ResolvedCredentials:
    env_vars = {
        key: _stand_in(mapping)
        for key, mapping in (server.credential_env_map or {}).items()
    }
    files = [
        CredentialFile(
            env_var=key,
            filename=spec["filename"],
            content=json.dumps(_render(spec["content"])),
            target=spec.get("target", "file"),
        )
        for key, spec in (server.credential_file_map or {}).items()
    ]
    return ResolvedCredentials(
        env_vars=env_vars, headers={}, used_credential_ids=[], files=files
    )


async def _handshake(server: MCPServer) -> list[str]:
    """Start the server the way a run does, and return its tool names."""
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    resolved = _resolved_for(server)
    directory, file_env = _write_credential_files(server, 1, resolved)
    try:
        if file_env:
            resolved = ResolvedCredentials(
                env_vars={**resolved.env_vars, **file_env},
                headers={}, used_credential_ids=[], files=[],
            )
        env = _build_subprocess_env(server, resolved)
        command = shutil.which(server.command) or server.command
        params = StdioServerParameters(
            command=command, args=server.args or [], env=env
        )
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await asyncio.wait_for(session.initialize(), timeout=HANDSHAKE_TIMEOUT)
                result = await asyncio.wait_for(
                    session.list_tools(), timeout=HANDSHAKE_TIMEOUT
                )
                return [t.name for t in result.tools]
    finally:
        _remove_credential_dir(directory)
        if directory:
            assert not os.path.exists(directory), (
                "The credential directory outlived the session. It holds a "
                "refresh token in plaintext."
            )


@unittest.skipUnless(LIVE, "set MCP_LIVE_TESTS=1 to start real connectors")
@override_settings(
    GOOGLE_OAUTH_CLIENT_ID="stand-in.apps.googleusercontent.com",
    GOOGLE_OAUTH_CLIENT_SECRET="stand-in-secret",
)
class LiveConnectorTests(TestCase):
    def _enabled(self):
        return MCPServer.objects.filter(user__isnull=True, enabled=True).order_by("name")

    def test_every_enabled_connector_starts_and_lists_its_tools(self):
        from asgiref.sync import async_to_sync

        for server in self._enabled():
            with self.subTest(connector=server.name):
                tools = async_to_sync(_handshake)(server)
                self.assertTrue(
                    tools,
                    f"{server.name} handshook but advertised no tools — the row "
                    f"claims a capability the server does not provide.",
                )

    def test_the_tool_count_matches_what_the_catalogue_claims(self):
        """
        `VERIFIED` records what each package advertised when someone checked.
        A drift is not necessarily a failure — packages add tools — but it must
        be a decision, not a surprise, because the number is what the offline
        test guards.
        """
        from asgiref.sync import async_to_sync

        for server in self._enabled():
            spec = next((a for a in (server.args or []) if not a.startswith("-")), None)
            expected = CuratedPackageTests.VERIFIED.get(spec)
            if expected is None:
                continue
            with self.subTest(connector=server.name, package=spec):
                tools = async_to_sync(_handshake)(server)
                self.assertEqual(
                    len(tools), expected,
                    f"{spec} now advertises {len(tools)} tools, not {expected}. "
                    f"Update CuratedPackageTests.VERIFIED if that is expected.",
                )

    def test_no_platform_secret_reaches_a_connector(self):
        """
        The allowlist, checked against the real rows rather than a fake server.
        A connector that needed one more variable would be fixed by widening
        `_ENV_PASSTHROUGH`, and this is what notices if the fix went too far.
        """
        forbidden = {
            "SECRET_KEY", "CREDENTIAL_ENCRYPTION_KEY", "POSTGRES_PASSWORD",
            "DATABASE_URL", "GOOGLE_OAUTH_CLIENT_SECRET",
        }
        for server in self._enabled():
            with self.subTest(connector=server.name):
                env = _build_subprocess_env(server, _resolved_for(server))
                self.assertEqual(forbidden & set(env), set())
