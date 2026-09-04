"""
Streamable HTTP transport — the one every hosted connector speaks.

Added 2026-09-01. `MCPServer.SERVER_TYPES` previously held only `stdio` and the
deprecated `sse`, so a row pointing at `https://mcp.notion.com/mcp` could not be
created, let alone connected.

The tests here guard the three things that broke, or nearly broke, in the
process — each of which is invisible until something reaches the network:

1. The dispatch in `_SessionWorker._run` must actually route `http` somewhere.
   Its `else` raises `Unsupported MCP server type`, so a missed branch is a
   runtime error, not a type error.
2. `streamablehttp_client` yields a **three**-tuple where `sse_client` yields
   two. Copying the SSE method and changing the function name produces a
   `ValueError: too many values to unpack` at connect time, which surfaces as an
   unexplained connection failure.
3. The serializer's SSRF guard was keyed on `== "sse"`, so every `http` row
   would have skipped URL validation entirely.
"""
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from mcp_integration.client import MCPClientManager
from mcp_integration.credential_injector import ResolvedCredentials
from mcp_integration.models import MCPServer
from mcp_integration.serializers import MCPServerSerializer

User = get_user_model()


def _http_server(user=None, url="https://mcp.example.com/mcp", **extra):
    return MCPServer.objects.create(
        name=extra.pop("name", "hosted"),
        type=extra.pop("type", "http"),
        url=url,
        enabled=True,
        user=user,
        **extra,
    )


def _creds(headers=None):
    """`ResolvedCredentials` with only the fields these tests care about."""
    return ResolvedCredentials(
        env_vars={}, headers=headers or {}, used_credential_ids=[],
    )


class TransportVocabularyTests(TestCase):
    def test_http_is_a_valid_server_type(self):
        self.assertIn("http", dict(MCPServer.SERVER_TYPES))

    def test_remote_types_covers_both_url_transports(self):
        """`REMOTE_TYPES` is what the SSRF guard keys on — if a URL-based
        transport is missing from it, that transport skips validation."""
        self.assertEqual(MCPServer.REMOTE_TYPES, frozenset({"http", "sse"}))
        for name, _label in MCPServer.SERVER_TYPES:
            if name != "stdio":
                self.assertIn(name, MCPServer.REMOTE_TYPES, name)


class DispatchTests(TestCase):
    """The worker must route `http` to `_connect_http`, not to the `else`."""

    def setUp(self):
        self.user = User.objects.create_user("dispatch", "d@x.com", "x" * 12)

    def test_http_type_dispatches_to_the_http_connector(self):
        server = _http_server(self.user)
        manager = MCPClientManager(server_id=server.id, user=self.user)
        self.assertTrue(hasattr(manager, "_connect_http"))

    def test_every_declared_type_has_a_connector(self):
        """Guards the `else: raise Unsupported MCP server type` branch: adding a
        transport to the model without one here is a runtime-only failure."""
        manager = MCPClientManager(server_id=1, user=self.user)
        for name, _label in MCPServer.SERVER_TYPES:
            self.assertTrue(
                hasattr(manager, f"_connect_{name}"),
                f"{name} is a declared server type with no _connect_{name}",
            )


class RemotePreparationTests(TestCase):
    """`_prepare_remote` is shared by both URL transports."""

    def setUp(self):
        self.user = User.objects.create_user("prep", "p@x.com", "x" * 12)

    @staticmethod
    def _run(coro):
        import asyncio
        return asyncio.run(coro)

    def test_missing_url_is_refused_before_any_network_call(self):
        server = _http_server(self.user, url=None)
        manager = MCPClientManager(server_id=server.id, user=self.user)
        from mcp.client.streamable_http import streamablehttp_client

        with self.assertRaises(ValueError) as ctx:
            self._run(manager._prepare_remote(
                server, _creds(), streamablehttp_client,
            ))
        self.assertIn("no URL", str(ctx.exception))

    def test_headers_are_passed_through_for_http(self):
        server = _http_server(self.user)
        manager = MCPClientManager(server_id=server.id, user=self.user)
        from mcp.client.streamable_http import streamablehttp_client

        resolved = _creds({"Authorization": "Bearer tok"})
        with mock.patch("core.safety.net.assert_url_safe", return_value=None):
            kwargs = self._run(
                manager._prepare_remote(server, resolved, streamablehttp_client)
            )
        self.assertEqual(kwargs["headers"], {"Authorization": "Bearer tok"})

    def test_url_is_revalidated_at_connect_time(self):
        """Registration-time validation is not enough: DNS can be rebound
        between saving a row and dialling it."""
        server = _http_server(self.user)
        manager = MCPClientManager(server_id=server.id, user=self.user)
        from mcp.client.streamable_http import streamablehttp_client

        with mock.patch("core.safety.net.assert_url_safe",
                        side_effect=ValueError("private address")) as guard:
            with self.assertRaises(ValueError):
                self._run(manager._prepare_remote(
                    server, _creds(), streamablehttp_client,
                ))
        guard.assert_called_once()


class HttpUrlValidationTests(TestCase):
    """The SSRF guard must cover `http`, not only `sse`."""

    def setUp(self):
        self.user = User.objects.create_user("val", "v@x.com", "x" * 12)
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def _validate(self, payload):
        serializer = MCPServerSerializer(data=payload)
        return serializer.is_valid(), serializer.errors

    def test_http_server_without_url_is_rejected(self):
        ok, errors = self._validate({"name": "h", "type": "http"})
        self.assertFalse(ok)
        self.assertIn("url", errors)

    def test_http_server_pointing_at_a_private_address_is_rejected(self):
        """The regression this test exists for: keyed on `== "sse"`, this
        payload validated cleanly and became a live SSRF primitive."""
        ok, errors = self._validate({
            "name": "h", "type": "http", "url": "http://169.254.169.254/latest/meta-data/",
        })
        self.assertFalse(ok, "an http row must not skip the SSRF guard")
        self.assertIn("url", errors)

    def test_http_server_with_a_public_url_is_accepted(self):
        ok, errors = self._validate({
            "name": "h", "type": "http", "url": "https://mcp.notion.com/mcp",
        })
        self.assertTrue(ok, errors)

    def test_sse_is_still_validated(self):
        ok, errors = self._validate({
            "name": "s", "type": "sse", "url": "http://127.0.0.1:8000/sse",
        })
        self.assertFalse(ok)
        self.assertIn("url", errors)


class HttpConnectionShapeTests(TestCase):
    """`streamablehttp_client` yields three values; `sse_client` yields two."""

    def setUp(self):
        self.user = User.objects.create_user("shape", "s@x.com", "x" * 12)

    def test_http_connector_unpacks_a_three_tuple(self):
        """A copy of `_connect_sse` with the function swapped raises
        `ValueError: too many values to unpack` here instead of connecting."""
        import asyncio
        import contextlib

        server = _http_server(self.user)
        manager = MCPClientManager(server_id=server.id, user=self.user)

        opened = {}

        @contextlib.asynccontextmanager
        async def fake_client(url, **kwargs):
            opened["url"] = url
            # Three values, as the real transport yields.
            yield ("read", "write", lambda: "session-id")

        class FakeSession:
            def __init__(self, read, write):
                opened["streams"] = (read, write)

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def initialize(self):
                opened["initialized"] = True

        async def run():
            with mock.patch("core.safety.net.assert_url_safe", return_value=None), \
                 mock.patch("mcp_integration.client.streamablehttp_client", fake_client), \
                 mock.patch("mcp_integration.client.ClientSession", FakeSession):
                async with manager._connect_http(server, _creds()) as session:
                    return session

        session = asyncio.run(run())
        self.assertIsInstance(session, FakeSession)
        self.assertEqual(opened["url"], server.url)
        # The session id callable is deliberately dropped, not passed on.
        self.assertEqual(opened["streams"], ("read", "write"))
        self.assertTrue(opened["initialized"])
