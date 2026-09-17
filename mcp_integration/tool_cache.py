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

        if isinstance(entry, dict) and isinstance(entry.get("tools"), list):
            return entry["tools"], time.time() >= entry.get("fresh_until", 0)

        # Redis knows nothing about this connection. Before this tier existed
        # that meant a cold `npx` start in front of the first token *and* a turn
        # with no connector tools at all. The stored listing answers in one
        # indexed read with a full toolbox, and is always reported stale so the
        # caller re-lists behind the answer and repopulates Redis.
        stored = await _stored_tools(server_id, user_id)
        return None if stored is None else (stored, True)

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
        await _store_tools(server_id, user_id, tools)

    @staticmethod
    async def invalidate(server_id: int, user_id: int | None = None) -> None:
        try:
            if user_id is not None:
                await sync_to_async(cache.delete)(_key(server_id, user_id))
            elif callable(delete_pattern := getattr(cache, "delete_pattern", None)):
                await sync_to_async(delete_pattern)(f"{KEY_PREFIX}{server_id}:user:*")
            else:
                # LocMemCache and several simple backends do not expose wildcard
                # deletion. v2 keys keep old entries short-lived and user-scoped.
                logger.debug("Cache backend cannot wildcard-delete MCP tools for server %s", server_id)
        except Exception as e:  # noqa: BLE001
            logger.warning("MCP tool cache invalidate failed for server %s: %s", server_id, e)
        # The stored copy goes too. This tier exists to survive *cache loss*,
        # never to survive an *edit*: a user who has just changed a connection
        # must not be answered from the list it had before, and falling through
        # to a live handshake here is exactly the behaviour that predates this
        # tier. `warm_cache` re-lists on save, so the window is one listing
        # long.
        await _forget_tools(server_id, user_id)


# ── The durable tier ─────────────────────────────────────────────────────────
#
# Every function below degrades to "no stored copy" rather than raising. A
# missing table (a deploy where migrations have not run yet) or a locked
# database must cost a turn its *fast path*, never its tools and never its
# answer — the same posture `disabled_tools_for` takes, and for the same
# reason.

def _read_stored(server_id: int, user_id: int | None) -> list[dict[str, Any]] | None:
    from .models import MCPToolCatalogue

    row = MCPToolCatalogue.objects.filter(
        server_id=server_id, user_id=user_id,
    ).values_list('tools', flat=True).first()
    return row if isinstance(row, list) else None


async def _stored_tools(server_id: int, user_id: int | None) -> list[dict[str, Any]] | None:
    try:
        return await sync_to_async(_read_stored)(server_id, user_id)
    except Exception as e:  # noqa: BLE001
        logger.warning("MCP tool catalogue read failed for server %s: %s", server_id, e)
        return None


def _write_stored(server_id: int, user_id: int | None, tools: list[dict[str, Any]]) -> None:
    from .models import MCPToolCatalogue

    MCPToolCatalogue.objects.update_or_create(
        server_id=server_id, user_id=user_id, defaults={'tools': tools},
    )


async def _store_tools(
    server_id: int, user_id: int | None, tools: list[dict[str, Any]]
) -> None:
    # An empty listing is not evidence of an empty server: every failure path
    # in `get_openai_tool_descriptors` returns `[]`, so storing one would write
    # "this connector has no tools" into the durable tier on the strength of a
    # timeout, and keep answering that way.
    if not tools:
        return
    try:
        await sync_to_async(_write_stored)(server_id, user_id, tools)
    except Exception as e:  # noqa: BLE001
        logger.warning("MCP tool catalogue write failed for server %s: %s", server_id, e)


def _delete_stored(server_id: int, user_id: int | None) -> None:
    from .models import MCPToolCatalogue

    rows = MCPToolCatalogue.objects.filter(server_id=server_id)
    if user_id is not None:
        rows = rows.filter(user_id=user_id)
    rows.delete()


async def _forget_tools(server_id: int, user_id: int | None) -> None:
    try:
        await sync_to_async(_delete_stored)(server_id, user_id)
    except Exception as e:  # noqa: BLE001
        logger.warning("MCP tool catalogue delete failed for server %s: %s", server_id, e)
