"""
MCPToolCache — Redis-backed cache of MCP `list_tools` responses.

Tool lists change rarely relative to how often they're read (every chat
message, every workflow palette open), so a short TTL eliminates a lot of
subprocess spin-up / HTTP round-trips.

An entry has **two** lifetimes, because a single TTL cannot serve both things
this cache is for. Freshness is about accuracy: after `SOFT_TTL_SECONDS` the
list is worth checking again. Presence is about latency: a stdio connector
costs an `npx` start to re-list, ~21 seconds cold, and that cost lands in front
of the user's first token. With one TTL the two were the same moment, so every
conversation with a two-minute pause in it paid a full reconnect *before*
answering — a cache that expired precisely when someone came back to their
work.

So a lapsed entry is served anyway, up to `HARD_TTL_SECONDS`, and marked stale
for the caller to refresh behind the answer. A tool list that is a few minutes
old is a much smaller problem than a turn that takes twenty seconds to start:
the list changes when a user edits a connection, and that path invalidates the
key outright rather than waiting for it to age out.
"""
from __future__ import annotations

import logging
import time
from typing import Any

from asgiref.sync import sync_to_async
from django.core.cache import cache

logger = logging.getLogger(__name__)

#: How long an entry is served without anyone re-listing behind it.
#:
#: 30 minutes: tool lists change when a user edits a connection, and that
#: path invalidates the key outright, so freshness here is not why anyone
#: waits — lengthening it only moves re-lists off the front of resumed
#: turns and into the background refresh. See the latency plan §2.
SOFT_TTL_SECONDS = 1800

#: How long a stale entry may still be served while a refresh runs. Beyond
#: this the entry is gone and the caller has to wait for a live listing.
#:
#: Was 1800 (half an hour), on the reasoning that a connector nobody had
#: touched for that long was as likely to be gone as slow. That reasoning
#: assumed a cache that rarely held anything older anyway — which was true
#: while `CACHES` was an unconfigured `LocMemCache`, because the *process*
#: usually died before the entry did. With a shared Redis cache the population
#: of half-hour-old entries is real and large, and every one of them is a
#: ~21-second cold `npx` in front of somebody's first token.
#:
#: A day is the right ceiling because being wrong is cheap and being slow is
#: not: a stale list costs one failed tool call, which the model sees and can
#: react to, while the miss costs twenty-one seconds of silence in front of an
#: answer. An entry is refreshed behind the response the moment it lapses the
#: soft TTL, and a user editing a connection invalidates the key outright, so
#: nothing here waits a day to notice a change someone made on purpose.
HARD_TTL_SECONDS = 86400

#: Retained under its old name: `TTL_SECONDS` was the whole contract before
#: there were two, and it means the same thing the soft one now does.
TTL_SECONDS = SOFT_TTL_SECONDS

# v3: entries carry their own freshness stamp, so the stored shape changed.
# Bumping the prefix retires the v2 bare lists rather than teaching every
# reader to recognise both.
KEY_PREFIX = "mcp_tools:v3:"


def _key(server_id: int, user_id: int | None) -> str:
    user_part = "system" if user_id is None else str(user_id)
    return f"{KEY_PREFIX}{server_id}:user:{user_part}"


class MCPToolCache:
    """Thin async wrapper around Django cache for MCP tool lists."""

    @staticmethod
    async def get_entry(
        server_id: int, user_id: int | None
    ) -> tuple[list[dict[str, Any]], bool] | None:
        """The cached tools and whether they are past their soft lifetime.

        The staleness flag is the whole point of the two lifetimes: it lets a
        caller answer now and re-list behind the answer. `get` stays the plain
        accessor for everyone who only wants the list.
        """
        try:
            entry = await sync_to_async(cache.get)(_key(server_id, user_id))
        except Exception as e:  # noqa: BLE001
            logger.warning("MCP tool cache get failed for server %s: %s", server_id, e)
            return None

        if not isinstance(entry, dict) or "tools" not in entry:
            return None
        tools = entry.get("tools")
        if not isinstance(tools, list):
            return None
        return tools, time.time() >= entry.get("fresh_until", 0)

    @staticmethod
    async def get(server_id: int, user_id: int | None) -> list[dict[str, Any]] | None:
        entry = await MCPToolCache.get_entry(server_id, user_id)
        return None if entry is None else entry[0]

    @staticmethod
    async def set(server_id: int, user_id: int | None, tools: list[dict[str, Any]]) -> None:
        try:
            await sync_to_async(cache.set)(
                _key(server_id, user_id),
                {"tools": tools, "fresh_until": time.time() + SOFT_TTL_SECONDS},
                HARD_TTL_SECONDS,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("MCP tool cache set failed for server %s: %s", server_id, e)

    @staticmethod
    async def invalidate(server_id: int, user_id: int | None = None) -> None:
        try:
            if user_id is not None:
                await sync_to_async(cache.delete)(_key(server_id, user_id))
                return

            pattern = f"{KEY_PREFIX}{server_id}:user:*"
            delete_pattern = getattr(cache, "delete_pattern", None)
            if callable(delete_pattern):
                await sync_to_async(delete_pattern)(pattern)
            else:
                # LocMemCache and several simple backends do not expose wildcard
                # deletion. v2 keys keep old entries short-lived and user-scoped.
                logger.debug("Cache backend cannot wildcard-delete MCP tools for server %s", server_id)
        except Exception as e:  # noqa: BLE001
            logger.warning("MCP tool cache invalidate failed for server %s: %s", server_id, e)
