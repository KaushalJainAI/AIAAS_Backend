"""Knowing which tools exist must never start a connector.

This is the rule the 2026-09-16 outage came down to. A chat turn asked eight
connectors what they could do; each answer was a `npx` start (two Node
processes) under a 5 s budget that a cold start — ~21 s — could never meet. So
the common case was a turn paying five seconds of silence *per connector* and
getting no tools for it, while spawning the processes that pushed a 384 MB
container into the OOM killer.

A tool list is a catalogue, and the catalogue is already on disk
(`MCPToolCatalogue`, and Redis in front of it). Listing reads it; a miss queues
a refresh behind the answer. **A connector starts when a tool is called**, and
not before.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

from asgiref.sync import async_to_sync
from django.test import SimpleTestCase

from mcp_integration.client import MCPClientManager
from mcp_integration.tool_provider import MCPToolProvider

TOOLS = [{"name": "search", "description": "find things", "inputSchema": {"type": "object"}}]


class _Server:
    id = 1
    name = "Fetch"


def _run(coro):
    return async_to_sync(lambda: coro)()


class ListingIsStorageOnlyTests(SimpleTestCase):
    def test_a_cached_listing_answers_without_touching_credentials(self):
        """Resolving credentials is the first step of a live listing."""
        manager = MCPClientManager(1, user=5)
        with patch("mcp_integration.client.MCPToolCache.get_entry",
                   new_callable=AsyncMock, return_value=(TOOLS, False)), \
             patch.object(MCPClientManager, "get_server_config", new_callable=AsyncMock), \
             patch.object(MCPClientManager, "_resolve_credentials",
                          new_callable=AsyncMock) as creds:
            tools = _run(manager.list_tools(cached_only=True))

        self.assertEqual(tools, TOOLS)
        creds.assert_not_called()

    def test_a_miss_returns_nothing_and_queues_a_refresh(self):
        """No tools this turn, and no subprocess either. The next turn has them."""
        manager = MCPClientManager(1, user=5)
        with patch("mcp_integration.client.MCPToolCache.get_entry",
                   new_callable=AsyncMock, return_value=None), \
             patch.object(MCPClientManager, "get_server_config", new_callable=AsyncMock), \
             patch.object(MCPClientManager, "_resolve_credentials",
                          new_callable=AsyncMock) as creds, \
             patch.object(MCPClientManager, "_session") as session, \
             patch("mcp_integration.client._refresh_in_background") as refresh:
            tools = _run(manager.list_tools(cached_only=True))

        self.assertEqual(tools, [])
        creds.assert_not_called()
        session.assert_not_called()
        refresh.assert_called_once()

    def test_the_descriptor_path_never_opens_a_session(self):
        """The agent/chat entry point, end to end: storage in, descriptors out."""
        with patch("mcp_integration.tool_provider.get_servers_for_user",
                   new_callable=AsyncMock, return_value=[_Server()]), \
             patch("mcp_integration.client.MCPToolCache.get_entry",
                   new_callable=AsyncMock, return_value=(TOOLS, False)), \
             patch.object(MCPClientManager, "get_server_config", new_callable=AsyncMock), \
             patch.object(MCPClientManager, "_session") as session:
            descriptors = _run(MCPToolProvider.get_openai_tool_descriptors(user=5))

        self.assertEqual(len(descriptors), 1)
        self.assertTrue(descriptors[0]["function"]["name"].startswith("mcp__1__"))
        session.assert_not_called()

    def test_a_cold_connector_costs_the_turn_no_tools_and_no_wait(self):
        """The case that used to cost 5s of dead air per connector."""
        with patch("mcp_integration.tool_provider.get_servers_for_user",
                   new_callable=AsyncMock, return_value=[_Server()]), \
             patch("mcp_integration.client.MCPToolCache.get_entry",
                   new_callable=AsyncMock, return_value=None), \
             patch.object(MCPClientManager, "get_server_config", new_callable=AsyncMock), \
             patch.object(MCPClientManager, "_session") as session, \
             patch("mcp_integration.client._refresh_in_background") as refresh:
            descriptors = _run(MCPToolProvider.get_openai_tool_descriptors(user=5))

        self.assertEqual(descriptors, [])
        session.assert_not_called()
        refresh.assert_called_once()
