"""
A lapsed tool list is served anyway, and refreshed behind the answer.

The failure this closes is invisible in every log: the cache held for 120
seconds, so a conversation with any pause in it came back to an expired entry
and paid a full `npx` start — measured at ~21 seconds cold — before the model
was even asked. Nothing errored. The user simply waited, and the turn that felt
slowest was the one they resumed after thinking.

Two lifetimes fix it, and the tests below pin both, because each is silent when
wrong: serve nothing stale and the latency comes back, serve stale for ever and
a connector the user edited keeps offering tools it no longer has.
"""
from __future__ import annotations

import time
from unittest.mock import AsyncMock, patch

from asgiref.sync import async_to_sync
from django.core.cache import cache
from django.test import SimpleTestCase

from mcp_integration import tool_cache
from mcp_integration.tool_cache import MCPToolCache

TOOLS = [{"name": "search", "description": "", "inputSchema": {}}]


def _run(coro):
    return async_to_sync(lambda: coro)()


class TwoLifetimeTests(SimpleTestCase):
    def setUp(self):
        cache.clear()

    def test_a_fresh_entry_is_not_stale(self):
        async_to_sync(MCPToolCache.set)(1, 5, TOOLS)
        tools, stale = async_to_sync(MCPToolCache.get_entry)(1, 5)
        self.assertEqual(tools, TOOLS)
        self.assertFalse(stale)

    def test_past_the_soft_lifetime_it_is_served_but_marked(self):
        """The whole point: still readable, and known to need a refresh."""
        with patch.object(tool_cache.time, "time",
                          return_value=time.time() - tool_cache.SOFT_TTL_SECONDS - 1):
            async_to_sync(MCPToolCache.set)(1, 5, TOOLS)

        tools, stale = async_to_sync(MCPToolCache.get_entry)(1, 5)
        self.assertEqual(tools, TOOLS, "a lapsed list must still answer")
        self.assertTrue(stale, "a lapsed list must ask to be refreshed")

    def test_get_still_returns_a_plain_list(self):
        """Every other caller only wants the tools, and still gets them."""
        async_to_sync(MCPToolCache.set)(1, 5, TOOLS)
        self.assertEqual(async_to_sync(MCPToolCache.get)(1, 5), TOOLS)

    def test_a_foreign_shape_is_ignored_rather_than_returned(self):
        """A v2 bare list left in a shared Redis must not be read as an entry."""
        cache.set(tool_cache._key(1, 5), TOOLS, 60)
        self.assertIsNone(async_to_sync(MCPToolCache.get_entry)(1, 5))
        self.assertIsNone(async_to_sync(MCPToolCache.get)(1, 5))


class RefreshBehindTheAnswerTests(SimpleTestCase):
    """`list_tools` answers from the stale copy and re-lists in the background."""

    def setUp(self):
        from mcp_integration import client

        client._refreshing.clear()

    def _list(self, entry):
        from mcp_integration.client import MCPClientManager

        manager = MCPClientManager(1, user=5)
        with patch("mcp_integration.client.MCPToolCache.get_entry",
                   new_callable=AsyncMock, return_value=entry):
            with patch.object(MCPClientManager, "get_server_config",
                              new_callable=AsyncMock):
                with patch.object(MCPClientManager, "_resolve_credentials",
                                  new_callable=AsyncMock) as creds:
                    with patch("mcp_integration.client._refresh_in_background") as bg:
                        result = _run(manager.list_tools())
        return result, creds, bg

    def test_a_fresh_hit_refreshes_nothing(self):
        result, creds, bg = self._list((TOOLS, False))
        self.assertEqual(result, TOOLS)
        bg.assert_not_called()
        creds.assert_not_called()

    def test_a_stale_hit_answers_immediately_and_schedules_a_refresh(self):
        result, creds, bg = self._list((TOOLS, True))
        self.assertEqual(result, TOOLS, "the caller waited for a re-list")
        # The credentials are the tell: resolving them is the first step of a
        # live listing, so if they were touched, the answer was not served
        # from cache.
        creds.assert_not_called()
        bg.assert_called_once()

    def test_one_refresh_is_scheduled_for_concurrent_callers(self):
        """Five workers on one stale entry must not start five subprocesses.

        Moving a stampede from the foreground to the background does not fix
        it; it only makes it harder to see.
        """
        from mcp_integration import client

        started = []

        def _fake_spawn(coro):
            started.append(coro)
            coro.close()  # never actually run it; we are counting intents

        with patch("mcp_integration.client.spawn", _fake_spawn):
            for _ in range(5):
                client._refresh_in_background(1, 5)

        self.assertEqual(len(started), 1)
