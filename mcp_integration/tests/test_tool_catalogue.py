"""The tool cache has a floor under it.

Redis was the only place a connection's tool list lived, so the fallback chain
was **Redis -> nothing**. A restart, an eviction, a deploy or the 24h hard TTL
lapsing all produced the same cliff: a cold `npx` start in front of the first
token *and* a turn that ran with no connector tools at all — slower and less
capable at the same time, with nothing able to explain why.

These pin the three properties that make the durable tier safe rather than
merely fast: a cache miss still yields a **full toolbox**, an **edit** still
falls through to a live listing, and a **failure** to read the tier costs the
fast path and nothing else.
"""
from __future__ import annotations

from unittest.mock import patch

from asgiref.sync import async_to_sync
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase

from mcp_integration.models import MCPServer, MCPToolCatalogue
from mcp_integration.tool_cache import MCPToolCache

User = get_user_model()

TOOLS = [{"name": "send_email", "description": "", "inputSchema": {}}]


def _run(coro):
    return async_to_sync(lambda: coro)()


class DurableCatalogueTests(TestCase):
    def setUp(self) -> None:
        cache.clear()
        self.addCleanup(cache.clear)
        self.user = User.objects.create_user(username="u", password="pw")
        self.server = MCPServer.objects.create(name="Gmail", type="stdio", command="npx")

    def _set(self, tools=TOOLS):
        async_to_sync(MCPToolCache.set)(self.server.id, self.user.id, tools)

    def _get_entry(self):
        return async_to_sync(MCPToolCache.get_entry)(self.server.id, self.user.id)

    # ── The write ──

    def test_a_listing_is_stored_durably_as_well_as_cached(self):
        self._set()
        row = MCPToolCatalogue.objects.get(server=self.server, user=self.user)
        self.assertEqual(row.tools, TOOLS)

    def test_an_empty_listing_is_never_stored(self):
        """Every failure path returns `[]`, so storing one would record a
        timeout as "this connector has no tools" and keep answering that way."""
        self._set(tools=[])
        self.assertFalse(MCPToolCatalogue.objects.exists())

    # ── The read ──

    def test_a_cache_miss_still_yields_a_full_toolbox(self):
        self._set()
        cache.clear()  # Redis restarted, evicted, or the hard TTL lapsed

        entry = self._get_entry()

        self.assertIsNotNone(entry, "the stored listing should have answered")
        tools, stale = entry
        self.assertEqual(tools, TOOLS)

    def test_a_stored_listing_always_reports_stale(self):
        """So the caller re-lists behind the answer and refills Redis.

        Reporting it fresh would leave the durable copy serving for ever and
        Redis permanently cold — the tier is a floor, not a replacement.
        """
        self._set()
        cache.clear()

        _tools, stale = self._get_entry()
        self.assertTrue(stale)

    def test_redis_still_wins_when_it_has_the_answer(self):
        self._set()
        MCPToolCatalogue.objects.update(tools=[{"name": "stale_copy"}])

        tools, stale = self._get_entry()

        self.assertEqual(tools, TOOLS)
        self.assertFalse(stale)

    def test_nothing_anywhere_is_still_a_miss(self):
        self.assertIsNone(self._get_entry())

    def test_one_user_s_listing_is_not_served_to_another(self):
        other = User.objects.create_user(username="other", password="pw")
        self._set()
        cache.clear()

        self.assertIsNone(
            async_to_sync(MCPToolCache.get_entry)(self.server.id, other.id)
        )

    # ── Invalidation ──

    def test_an_edit_clears_the_stored_copy_too(self):
        """This tier survives *cache loss*, never an *edit*.

        A user who has just changed a connection must not be answered from the
        list it had before, so invalidation drops both tiers and the next read
        falls through to a live handshake — exactly the behaviour that predates
        this tier.
        """
        self._set()
        async_to_sync(MCPToolCache.invalidate)(self.server.id, self.user.id)

        self.assertFalse(MCPToolCatalogue.objects.exists())
        self.assertIsNone(self._get_entry())

    def test_invalidating_a_whole_server_clears_every_user_s_copy(self):
        other = User.objects.create_user(username="other", password="pw")
        self._set()
        async_to_sync(MCPToolCache.set)(self.server.id, other.id, TOOLS)

        async_to_sync(MCPToolCache.invalidate)(self.server.id)

        self.assertFalse(MCPToolCatalogue.objects.exists())

    def test_deleting_a_connection_takes_its_catalogue(self):
        self._set()
        self.server.delete()
        self.assertFalse(MCPToolCatalogue.objects.exists())

    # ── Degradation ──

    def test_a_broken_durable_tier_costs_the_fast_path_and_nothing_else(self):
        """A missing table or a locked database must not fail a turn.

        The same posture `disabled_tools_for` takes: a failed read means "no
        stored copy", never "no tools".
        """
        self._set()
        cache.clear()

        with patch("mcp_integration.tool_cache._read_stored", side_effect=OSError("locked")):
            self.assertIsNone(self._get_entry())

    def test_a_failed_store_does_not_break_the_listing(self):
        with patch("mcp_integration.tool_cache._write_stored", side_effect=OSError("locked")):
            self._set()  # must not raise

        tools, stale = self._get_entry()
        self.assertEqual(tools, TOOLS)


class SystemLevelCatalogueTests(TestCase):
    """`user_id` is None for a system listing — the case `_coerce_user_id`
    already returns None for, and the row a shared baseline would use if the
    per-user question is ever settled that way."""

    def setUp(self) -> None:
        cache.clear()
        self.addCleanup(cache.clear)
        self.server = MCPServer.objects.create(name="Curated", type="stdio", command="npx")

    def test_a_system_listing_round_trips(self):
        async_to_sync(MCPToolCache.set)(self.server.id, None, TOOLS)
        cache.clear()

        tools, stale = async_to_sync(MCPToolCache.get_entry)(self.server.id, None)

        self.assertEqual(tools, TOOLS)
        self.assertTrue(stale)

    def test_only_one_system_row_can_exist_per_server(self):
        """SQL treats NULLs as distinct, so `unique_together` would have
        allowed a hundred of these. The partial constraint is what stops it."""
        async_to_sync(MCPToolCache.set)(self.server.id, None, TOOLS)
        async_to_sync(MCPToolCache.set)(self.server.id, None, [{"name": "second"}])

        rows = MCPToolCatalogue.objects.filter(server=self.server, user__isnull=True)
        self.assertEqual(rows.count(), 1)
        self.assertEqual(rows.first().tools, [{"name": "second"}])
