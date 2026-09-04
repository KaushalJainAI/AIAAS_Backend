"""
Comprehensive unit tests for every mcp_integration service layer.

Coverage targets
----------------
tool_cache     — get / set / invalidate (user-scoped + wildcard), graceful failure
tool_provider  — encode/decode round-trip, is_mcp_tool, descriptor shape,
                 execute (success, not-found, credential errors, tool error),
                 get_openai_tool_descriptors (skip bad servers)
client         — _serialise_tool_result, list_tools (cache hit / miss, single
                 resolution), call_tool (success + isError), access control,
                 drain_pool, get_servers_for_user
models         — __str__, unique_together enforcement
"""
from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from asgiref.sync import async_to_sync
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.exceptions import PermissionDenied
from django.db import IntegrityError
from django.test import SimpleTestCase, TestCase

from mcp_integration.client import (
    MCPClientManager,
    _serialise_tool_result,
    drain_pool,
    get_servers_for_user,
    _pool,
)
from mcp_integration.credential_injector import CredentialInvalidError, CredentialMissingError
from mcp_integration.models import MCPServer
from mcp_integration.tool_cache import MCPToolCache
from mcp_integration.tool_provider import (
    MCPToolProvider,
    _build_openai_descriptor,
    decode_tool_name,
    encode_tool_name,
    is_mcp_tool,
)

User = get_user_model()


def _run(coro):
    return asyncio.run(coro)


# ─────────────────────────────────────────────────────────────────────────────
# Helpers / fakes
# ─────────────────────────────────────────────────────────────────────────────

def _fake_server(*, name="srv", server_type="stdio", required=None, env_map=None, header_map=None):
    s = MagicMock(spec=MCPServer)
    s.id = 1
    s.name = name
    s.type = server_type
    s.required_credential_types = required or []
    s.credential_env_map = env_map or {}
    s.credential_header_map = header_map or {}
    return s


def _content_block(type_: str, **kwargs):
    block = SimpleNamespace(type=type_, **kwargs)
    return block


def _call_result(blocks, *, is_error=False):
    r = MagicMock()
    r.isError = is_error
    r.content = blocks
    return r


# ─────────────────────────────────────────────────────────────────────────────
# MCPToolCache
# ─────────────────────────────────────────────────────────────────────────────

class ToolCacheGetSetTests(TestCase):
    def setUp(self):
        cache.clear()

    def tearDown(self):
        cache.clear()

    def test_get_returns_none_on_empty(self):
        self.assertIsNone(_run(MCPToolCache.get(99, 1)))

    def test_set_then_get_round_trip(self):
        tools = [{"name": "search", "description": "Search files"}]
        _run(MCPToolCache.set(1, 2, tools))
        self.assertEqual(_run(MCPToolCache.get(1, 2)), tools)

    def test_system_user_key_is_separate(self):
        _run(MCPToolCache.set(1, None, [{"name": "sys"}]))
        self.assertIsNone(_run(MCPToolCache.get(1, 42)))

    def test_different_server_ids_dont_collide(self):
        _run(MCPToolCache.set(1, 5, [{"name": "a"}]))
        _run(MCPToolCache.set(2, 5, [{"name": "b"}]))
        self.assertEqual(_run(MCPToolCache.get(1, 5)), [{"name": "a"}])
        self.assertEqual(_run(MCPToolCache.get(2, 5)), [{"name": "b"}])

    def test_get_failure_returns_none_gracefully(self):
        with patch("mcp_integration.tool_cache.cache.get", side_effect=Exception("redis down")):
            result = _run(MCPToolCache.get(1, 1))
        self.assertIsNone(result)

    def test_set_failure_is_silent(self):
        with patch("mcp_integration.tool_cache.cache.set", side_effect=Exception("redis down")):
            # Must not raise
            _run(MCPToolCache.set(1, 1, []))


class ToolCacheInvalidateTests(TestCase):
    def setUp(self):
        cache.clear()

    def tearDown(self):
        cache.clear()

    def test_invalidate_user_scoped_removes_only_that_entry(self):
        _run(MCPToolCache.set(1, 10, [{"name": "a"}]))
        _run(MCPToolCache.set(1, 20, [{"name": "b"}]))
        _run(MCPToolCache.invalidate(1, user_id=10))
        self.assertIsNone(_run(MCPToolCache.get(1, 10)))
        self.assertEqual(_run(MCPToolCache.get(1, 20)), [{"name": "b"}])

    def test_invalidate_without_user_attempts_wildcard(self):
        _run(MCPToolCache.set(1, 10, [{"name": "x"}]))
        # LocMemCache doesn't support delete_pattern → gracefully logged, no crash
        _run(MCPToolCache.invalidate(1))

    def test_invalidate_failure_is_silent(self):
        with patch("mcp_integration.tool_cache.cache.delete", side_effect=Exception("boom")):
            _run(MCPToolCache.invalidate(1, user_id=5))


# ─────────────────────────────────────────────────────────────────────────────
# _serialise_tool_result
# ─────────────────────────────────────────────────────────────────────────────

class SerialiseToolResultTests(SimpleTestCase):
    def test_single_text_returns_string(self):
        result = _call_result([_content_block("text", text="hello")])
        self.assertEqual(_serialise_tool_result(result), "hello")

    def test_multiple_texts_returns_list(self):
        result = _call_result([
            _content_block("text", text="a"),
            _content_block("text", text="b"),
        ])
        self.assertEqual(_serialise_tool_result(result), ["a", "b"])

    def test_image_block_returns_dict(self):
        result = _call_result([_content_block("image", mimeType="image/png", data="abc==")])
        out = _serialise_tool_result(result)
        self.assertEqual(out["type"], "image")
        self.assertEqual(out["mime_type"], "image/png")
        self.assertEqual(out["data"], "abc==")

    def test_resource_block_returns_dict(self):
        res = SimpleNamespace(uri="file:///tmp/x", mimeType="text/plain", text="content")
        result = _call_result([_content_block("resource", resource=res)])
        out = _serialise_tool_result(result)
        self.assertEqual(out["type"], "resource")
        self.assertEqual(out["uri"], "file:///tmp/x")
        self.assertEqual(out["text"], "content")

    def test_unknown_type_falls_back_to_str(self):
        result = _call_result([_content_block("binary", data=b"\x00")])
        out = _serialise_tool_result(result)
        self.assertIsInstance(out, str)

    def test_mixed_content_returns_list(self):
        result = _call_result([
            _content_block("text", text="label"),
            _content_block("image", mimeType="image/jpeg", data="xyz"),
        ])
        out = _serialise_tool_result(result)
        self.assertIsInstance(out, list)
        self.assertEqual(len(out), 2)


# ─────────────────────────────────────────────────────────────────────────────
# Tool name encoding / decoding
# ─────────────────────────────────────────────────────────────────────────────

class ToolNameEncodingTests(SimpleTestCase):
    def test_encode_is_schema_safe(self):
        name = encode_tool_name(5, "some.tool/name")
        self.assertRegex(name, r"^[a-zA-Z0-9_\-]{1,64}$")

    def test_encode_respects_max_length(self):
        self.assertLessEqual(len(encode_tool_name(999, "x" * 300)), 64)

    def test_encode_is_deterministic(self):
        self.assertEqual(
            encode_tool_name(1, "search"),
            encode_tool_name(1, "search"),
        )

    def test_encode_is_collision_resistant(self):
        # Same characters, different separators → different encoded names
        self.assertNotEqual(
            encode_tool_name(1, "foo.bar"),
            encode_tool_name(1, "foo_bar"),
        )

    def test_decode_returns_server_id_and_suffix(self):
        encoded = encode_tool_name(42, "my_tool")
        decoded = decode_tool_name(encoded)
        self.assertIsNotNone(decoded)
        self.assertEqual(decoded[0], 42)

    def test_decode_non_mcp_name_returns_none(self):
        self.assertIsNone(decode_tool_name("plain_tool_name"))
        self.assertIsNone(decode_tool_name("mcp__notanint__tool"))

    def test_is_mcp_tool_positive(self):
        self.assertTrue(is_mcp_tool(encode_tool_name(1, "x")))

    def test_is_mcp_tool_negative(self):
        self.assertFalse(is_mcp_tool("web_search"))
        self.assertFalse(is_mcp_tool(""))


# ─────────────────────────────────────────────────────────────────────────────
# _build_openai_descriptor
# ─────────────────────────────────────────────────────────────────────────────

class BuildOpenAIDescriptorTests(SimpleTestCase):
    def _server(self, sid=7, name="TestSrv"):
        s = MagicMock()
        s.id = sid
        s.name = name
        return s

    def test_output_shape(self):
        server = self._server()
        tool = {
            "name": "search",
            "description": "Search files",
            "inputSchema": {"type": "object", "properties": {"q": {"type": "string"}}},
        }
        desc = _build_openai_descriptor(server, tool)
        self.assertEqual(desc["type"], "function")
        fn = desc["function"]
        self.assertIn("name", fn)
        self.assertIn("description", fn)
        self.assertIn("parameters", fn)
        self.assertEqual(fn["parameters"]["type"], "object")

    def test_schema_without_object_type_is_normalised(self):
        server = self._server()
        tool = {"name": "t", "description": "", "inputSchema": {"type": "array"}}
        desc = _build_openai_descriptor(server, tool)
        self.assertEqual(desc["function"]["parameters"]["type"], "object")

    def test_server_name_appears_in_description(self):
        server = self._server(name="MyApp")
        tool = {"name": "ping", "description": "Ping service", "inputSchema": {}}
        desc = _build_openai_descriptor(server, tool)
        self.assertIn("MyApp", desc["function"]["description"])

    def test_missing_description_uses_fallback(self):
        server = self._server(name="X")
        tool = {"name": "act", "description": None, "inputSchema": {}}
        desc = _build_openai_descriptor(server, tool)
        self.assertIn("act", desc["function"]["description"])


# ─────────────────────────────────────────────────────────────────────────────
# MCPToolProvider.execute
# ─────────────────────────────────────────────────────────────────────────────

class ToolProviderExecuteTests(SimpleTestCase):
    def _encoded(self, sid=1, tool="search"):
        return encode_tool_name(sid, tool)

    def test_unknown_tool_returns_error_json(self):
        name = self._encoded(99, "ghost")
        with patch.object(MCPToolProvider, "_resolve_binding", new_callable=AsyncMock, return_value=None):
            result = _run(MCPToolProvider.execute(name, {}, user=1))
        data = json.loads(result)
        self.assertEqual(data["code"], "tool_not_found")

    def test_credential_missing_returns_error_json(self):
        name = self._encoded(1, "search")
        with patch.object(MCPToolProvider, "_resolve_binding", new_callable=AsyncMock,
                          side_effect=CredentialMissingError("github", "srv")):
            result = _run(MCPToolProvider.execute(name, {}, user=1))
        data = json.loads(result)
        self.assertEqual(data["code"], "credential_missing")

    def test_credential_invalid_returns_error_json(self):
        name = self._encoded(1, "search")
        with patch.object(MCPToolProvider, "_resolve_binding", new_callable=AsyncMock,
                          side_effect=CredentialInvalidError("bad key")):
            result = _run(MCPToolProvider.execute(name, {}, user=1))
        data = json.loads(result)
        self.assertEqual(data["code"], "credential_invalid")

    def test_permission_denied_returns_not_found_json(self):
        name = self._encoded(1, "search")
        with patch.object(MCPToolProvider, "_resolve_binding", new_callable=AsyncMock,
                          side_effect=PermissionDenied()):
            result = _run(MCPToolProvider.execute(name, {}, user=1))
        data = json.loads(result)
        self.assertEqual(data["code"], "tool_not_found")

    def test_tool_error_returns_error_json(self):
        name = self._encoded(1, "search")
        from mcp_integration.tool_provider import _ToolBinding
        binding = _ToolBinding(server_id=1, server_name="X", original_tool_name="search")
        with patch.object(MCPToolProvider, "_resolve_binding", new_callable=AsyncMock, return_value=binding):
            with patch.object(MCPClientManager, "call_tool", new_callable=AsyncMock,
                              side_effect=RuntimeError("MCP tool 'search' reported error")):
                result = _run(MCPToolProvider.execute(name, {}, user=1))
        data = json.loads(result)
        self.assertEqual(data["code"], "tool_error")

    def test_string_result_returned_verbatim(self):
        name = self._encoded(1, "search")
        from mcp_integration.tool_provider import _ToolBinding
        binding = _ToolBinding(server_id=1, server_name="X", original_tool_name="search")
        with patch.object(MCPToolProvider, "_resolve_binding", new_callable=AsyncMock, return_value=binding):
            with patch.object(MCPClientManager, "call_tool", new_callable=AsyncMock, return_value="plain text"):
                result = _run(MCPToolProvider.execute(name, {}, user=1))
        self.assertEqual(result, "plain text")

    def test_dict_result_is_json_encoded(self):
        name = self._encoded(1, "search")
        from mcp_integration.tool_provider import _ToolBinding
        binding = _ToolBinding(server_id=1, server_name="X", original_tool_name="search")
        with patch.object(MCPToolProvider, "_resolve_binding", new_callable=AsyncMock, return_value=binding):
            with patch.object(MCPClientManager, "call_tool", new_callable=AsyncMock,
                              return_value={"files": ["a.txt"]}):
                result = _run(MCPToolProvider.execute(name, {}, user=1))
        self.assertEqual(json.loads(result), {"files": ["a.txt"]})


# ─────────────────────────────────────────────────────────────────────────────
# MCPToolProvider.get_openai_tool_descriptors
# ─────────────────────────────────────────────────────────────────────────────

class ToolProviderDescriptorsTests(SimpleTestCase):
    def _mock_server(self, sid, name):
        s = MagicMock()
        s.id = sid
        s.name = name
        return s

    def test_skips_server_with_missing_credential(self):
        servers = [self._mock_server(1, "GitHub")]
        with patch("mcp_integration.tool_provider.get_servers_for_user",
                   new_callable=AsyncMock, return_value=servers):
            with patch.object(MCPClientManager, "list_tools", new_callable=AsyncMock,
                              side_effect=CredentialMissingError("github", "GitHub")):
                result = _run(MCPToolProvider.get_openai_tool_descriptors(user=1))
        self.assertEqual(result, [])

    def test_skips_server_with_generic_error(self):
        servers = [self._mock_server(1, "Broken")]
        with patch("mcp_integration.tool_provider.get_servers_for_user",
                   new_callable=AsyncMock, return_value=servers):
            with patch.object(MCPClientManager, "list_tools", new_callable=AsyncMock,
                              side_effect=ConnectionError("refused")):
                result = _run(MCPToolProvider.get_openai_tool_descriptors(user=1))
        self.assertEqual(result, [])

    def test_returns_descriptors_for_valid_servers(self):
        server = self._mock_server(5, "FileSystem")
        tools = [{"name": "read", "description": "Read file", "inputSchema": {"type": "object", "properties": {}}}]
        with patch("mcp_integration.tool_provider.get_servers_for_user",
                   new_callable=AsyncMock, return_value=[server]):
            with patch.object(MCPClientManager, "list_tools", new_callable=AsyncMock, return_value=tools):
                result = _run(MCPToolProvider.get_openai_tool_descriptors(user=1))
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["type"], "function")

    def test_partial_failure_still_returns_working_servers(self):
        s1 = self._mock_server(1, "Good")
        s2 = self._mock_server(2, "Bad")
        tools = [{"name": "do", "description": "do it", "inputSchema": {"type": "object", "properties": {}}}]

        async def list_tools_side_effect(self_obj):
            if self_obj.server_id == 2:
                raise ConnectionError("down")
            return tools

        with patch("mcp_integration.tool_provider.get_servers_for_user",
                   new_callable=AsyncMock, return_value=[s1, s2]):
            with patch.object(MCPClientManager, "list_tools", list_tools_side_effect):
                result = _run(MCPToolProvider.get_openai_tool_descriptors(user=1))
        self.assertEqual(len(result), 1)


# ─────────────────────────────────────────────────────────────────────────────
# MCPClientManager — list_tools (single credential resolution)
# ─────────────────────────────────────────────────────────────────────────────

class ClientManagerListToolsTests(SimpleTestCase):
    def _manager(self, server_id=1, user_id=5):
        return MCPClientManager(server_id, user=user_id)

    def test_cache_hit_returns_cached_without_resolving_credentials(self):
        cached = [{"name": "search"}]
        manager = self._manager()
        # `(tools, stale)`: a fresh hit, so nothing is re-listed behind it.
        with patch("mcp_integration.client.MCPToolCache.get_entry",
                   new_callable=AsyncMock,
                   return_value=(cached, False)) as mock_cache:
            with patch.object(MCPClientManager, "_resolve_credentials",
                              new_callable=AsyncMock) as mock_creds:
                with patch.object(MCPClientManager, "get_server_config",
                                  new_callable=AsyncMock, return_value=_fake_server()):
                    result = _run(manager.list_tools())
        self.assertEqual(result, cached)
        mock_creds.assert_not_called()
        mock_cache.assert_called_once()

    def test_cache_miss_resolves_credentials_exactly_once(self):
        fake_srv = _fake_server()
        fake_resolved = MagicMock(env_vars={}, headers={})
        fake_tools = [{"name": "t", "description": "", "inputSchema": {"type": "object", "properties": {}}}]

        fake_session = AsyncMock()
        fake_session.list_tools = AsyncMock(return_value=MagicMock(
            tools=[SimpleNamespace(name="t", description="", inputSchema={"type": "object", "properties": {}})]
        ))

        @asynccontextmanager
        async def mock_session(self_obj, server, resolved):
            yield fake_session

        with patch("mcp_integration.client.MCPToolCache.get",
                   new_callable=AsyncMock, return_value=None):
            with patch("mcp_integration.client.MCPToolCache.set",
                       new_callable=AsyncMock):
                with patch.object(MCPClientManager, "get_server_config",
                                  new_callable=AsyncMock, return_value=fake_srv):
                    with patch.object(MCPClientManager, "_resolve_credentials",
                                      new_callable=AsyncMock, return_value=fake_resolved) as mock_creds:
                        with patch.object(MCPClientManager, "_session", mock_session):
                            result = _run(MCPClientManager(1, user=5).list_tools())

        # Credentials resolved exactly once — the core fix
        mock_creds.assert_called_once()
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["name"], "t")

    def test_cache_miss_stores_result(self):
        fake_srv = _fake_server()
        fake_resolved = MagicMock(env_vars={}, headers={})

        fake_session = AsyncMock()
        fake_session.list_tools = AsyncMock(return_value=MagicMock(
            tools=[SimpleNamespace(name="ping", description="pong", inputSchema=None)]
        ))

        @asynccontextmanager
        async def mock_session(self_obj, server, resolved):
            yield fake_session

        with patch("mcp_integration.client.MCPToolCache.get",
                   new_callable=AsyncMock, return_value=None):
            with patch("mcp_integration.client.MCPToolCache.set",
                       new_callable=AsyncMock) as mock_set:
                with patch.object(MCPClientManager, "get_server_config",
                                  new_callable=AsyncMock, return_value=fake_srv):
                    with patch.object(MCPClientManager, "_resolve_credentials",
                                      new_callable=AsyncMock, return_value=fake_resolved):
                        with patch.object(MCPClientManager, "_session", mock_session):
                            _run(MCPClientManager(1, user=5).list_tools())

        mock_set.assert_called_once()
        stored = mock_set.call_args[0][2]
        self.assertEqual(stored[0]["name"], "ping")
        # inputSchema None is normalised to empty object
        self.assertEqual(stored[0]["inputSchema"], {"type": "object", "properties": {}})


# ─────────────────────────────────────────────────────────────────────────────
# MCPClientManager — call_tool
# ─────────────────────────────────────────────────────────────────────────────

class ClientManagerCallToolTests(SimpleTestCase):
    def test_is_error_raises_runtime_error(self):
        fake_srv = _fake_server()
        fake_resolved = MagicMock(env_vars={}, headers={})
        error_result = _call_result([], is_error=True)

        fake_session = AsyncMock()
        fake_session.call_tool = AsyncMock(return_value=error_result)

        @asynccontextmanager
        async def mock_session(self_obj, server, resolved):
            yield fake_session

        with patch.object(MCPClientManager, "get_server_config",
                          new_callable=AsyncMock, return_value=fake_srv):
            with patch.object(MCPClientManager, "_resolve_credentials",
                              new_callable=AsyncMock, return_value=fake_resolved):
                with patch.object(MCPClientManager, "_session", mock_session):
                    with self.assertRaises(RuntimeError) as ctx:
                        _run(MCPClientManager(1, user=5).call_tool("search", {"q": "x"}))

        self.assertIn("search", str(ctx.exception))

    def test_success_returns_serialised_content(self):
        fake_srv = _fake_server()
        fake_resolved = MagicMock(env_vars={}, headers={})
        ok_result = _call_result([_content_block("text", text="found it")])

        fake_session = AsyncMock()
        fake_session.call_tool = AsyncMock(return_value=ok_result)

        @asynccontextmanager
        async def mock_session(self_obj, server, resolved):
            yield fake_session

        with patch.object(MCPClientManager, "get_server_config",
                          new_callable=AsyncMock, return_value=fake_srv):
            with patch.object(MCPClientManager, "_resolve_credentials",
                              new_callable=AsyncMock, return_value=fake_resolved):
                with patch.object(MCPClientManager, "_session", mock_session):
                    result = _run(MCPClientManager(1, user=5).call_tool("search", {}))

        self.assertEqual(result, "found it")

    def test_resolves_credentials_exactly_once(self):
        fake_srv = _fake_server()
        fake_resolved = MagicMock(env_vars={}, headers={})
        ok_result = _call_result([_content_block("text", text="ok")])

        fake_session = AsyncMock()
        fake_session.call_tool = AsyncMock(return_value=ok_result)

        @asynccontextmanager
        async def mock_session(self_obj, server, resolved):
            yield fake_session

        with patch.object(MCPClientManager, "get_server_config",
                          new_callable=AsyncMock, return_value=fake_srv):
            with patch.object(MCPClientManager, "_resolve_credentials",
                              new_callable=AsyncMock, return_value=fake_resolved) as mock_creds:
                with patch.object(MCPClientManager, "_session", mock_session):
                    _run(MCPClientManager(1, user=5).call_tool("search", {}))

        mock_creds.assert_called_once()


# ─────────────────────────────────────────────────────────────────────────────
# drain_pool
# ─────────────────────────────────────────────────────────────────────────────

class DrainPoolTests(SimpleTestCase):
    def test_drain_clears_pool(self):
        # Inject a fake entry directly into the module-level pool. A pooled
        # session is closed through its worker, not through the exit stack
        # directly: the worker is the task that opened it, and an anyio task
        # group may only be exited by that task.
        fake_worker = AsyncMock()
        fake_entry = MagicMock()
        fake_entry.worker = fake_worker
        _pool[(999, 1)] = fake_entry

        _run(drain_pool())

        self.assertNotIn((999, 1), _pool)
        fake_worker.close.assert_awaited_once()

    def test_drain_is_idempotent(self):
        # Already empty pool — must not raise
        _run(drain_pool())
        _run(drain_pool())


# ─────────────────────────────────────────────────────────────────────────────
# get_servers_for_user (DB-backed)
# ─────────────────────────────────────────────────────────────────────────────

class GetServersForUserTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="u1", password="pw")
        self.other = User.objects.create_user(username="u2", password="pw")

        self.system_server = MCPServer.objects.create(
            name="System FS", type="stdio", command="npx", user=None, enabled=True,
        )
        self.user_server = MCPServer.objects.create(
            name="My Server", type="stdio", command="npx", user=self.user, enabled=True,
        )
        self.other_server = MCPServer.objects.create(
            name="Other Server", type="stdio", command="npx", user=self.other, enabled=True,
        )
        self.disabled_server = MCPServer.objects.create(
            name="Disabled", type="stdio", command="npx", user=self.user, enabled=False,
        )

    # async_to_sync reuses Django's test transaction; asyncio.run() would open
    # a second connection and deadlock SQLite's table-level write lock.

    def test_user_sees_own_and_system_but_not_others(self):
        servers = async_to_sync(get_servers_for_user)(self.user)
        names = {s.name for s in servers}
        self.assertIn("System FS", names)
        self.assertIn("My Server", names)
        self.assertNotIn("Other Server", names)

    def test_disabled_servers_are_excluded(self):
        servers = async_to_sync(get_servers_for_user)(self.user)
        names = {s.name for s in servers}
        self.assertNotIn("Disabled", names)

    def test_none_user_sees_only_system_servers(self):
        servers = async_to_sync(get_servers_for_user)(None)
        names = {s.name for s in servers}
        self.assertIn("System FS", names)
        self.assertNotIn("My Server", names)


# ─────────────────────────────────────────────────────────────────────────────
# MCPServer model constraints
# ─────────────────────────────────────────────────────────────────────────────

class MCPServerModelTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="m_user", password="pw")

    def test_str_includes_name_and_type(self):
        s = MCPServer(name="GitHub", type="stdio")
        self.assertIn("GitHub", str(s))
        self.assertIn("stdio", str(s))

    def test_two_users_can_have_same_server_name(self):
        other = User.objects.create_user(username="m_other", password="pw")
        MCPServer.objects.create(name="MyServer", type="stdio", command="npx", user=self.user)
        # Must not raise
        MCPServer.objects.create(name="MyServer", type="stdio", command="npx", user=other)

    def test_same_user_cannot_have_duplicate_server_name(self):
        MCPServer.objects.create(name="Dup", type="stdio", command="npx", user=self.user)
        with self.assertRaises(IntegrityError):
            MCPServer.objects.create(name="Dup", type="stdio", command="npx", user=self.user)

    def test_multiple_system_servers_with_same_name_allowed_by_db(self):
        # unique_together with user=NULL is not enforced in SQLite/Postgres for NULLs
        MCPServer.objects.create(name="SysA", type="stdio", command="npx", user=None)
        # Second system server with same name — SQLite allows it; we don't block it here
        # (admin is responsible for keeping system names unique)
        MCPServer.objects.create(name="SysA", type="stdio", command="npx", user=None)
