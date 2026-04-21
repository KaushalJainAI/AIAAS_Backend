"""
MCPToolCache — Redis-backed cache of MCP `list_tools` responses.

Tool lists change rarely relative to how often they're read (every chat
message, every workflow palette open), so a short TTL eliminates a lot of
subprocess spin-up / HTTP round-trips.
"""
from __future__ import annotations

import logging
from typing import Any

from asgiref.sync import sync_to_async
from django.core.cache import cache

logger = logging.getLogger(__name__)

TTL_SECONDS = 120
KEY_PREFIX = "mcp_tools:v1:"


def _key(server_id: int) -> str:
    return f"{KEY_PREFIX}{server_id}"


class MCPToolCache:
    """Thin async wrapper around Django cache for MCP tool lists."""

    @staticmethod
    async def get(server_id: int) -> list[dict[str, Any]] | None:
        try:
            return await sync_to_async(cache.get)(_key(server_id))
        except Exception as e:  # noqa: BLE001
            logger.warning("MCP tool cache get failed for server %s: %s", server_id, e)
            return None

    @staticmethod
    async def set(server_id: int, tools: list[dict[str, Any]]) -> None:
        try:
            await sync_to_async(cache.set)(_key(server_id), tools, TTL_SECONDS)
        except Exception as e:  # noqa: BLE001
            logger.warning("MCP tool cache set failed for server %s: %s", server_id, e)

    @staticmethod
    async def invalidate(server_id: int) -> None:
        try:
            await sync_to_async(cache.delete)(_key(server_id))
        except Exception as e:  # noqa: BLE001
            logger.warning("MCP tool cache invalidate failed for server %s: %s", server_id, e)
