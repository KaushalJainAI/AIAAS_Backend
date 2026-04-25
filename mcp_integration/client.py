"""
MCPClientManager — opens (and pools) connections to an MCP server, exposing
`list_tools` / `call_tool`.

Connection pooling
------------------
Opening a fresh connection on every tool call is expensive:
  * stdio  — spawns a new subprocess + MCP handshake (~100–500 ms)
  * SSE    — TCP connect + HTTP upgrade + MCP handshake

The module-level `_pool` keeps one live `ClientSession` per
(server_id, user_id) pair for up to SESSION_TTL seconds.  An asyncio.Lock
per pool-entry serialises concurrent callers so the session is never used
by two coroutines simultaneously (MCP sessions are not concurrency-safe).

If a session errors mid-call it is evicted so the next call gets a fresh one.
"""
from __future__ import annotations

import contextlib
import inspect
import logging
import os
import shutil
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

import asyncio

from asgiref.sync import sync_to_async
from django.core.exceptions import PermissionDenied
from django.db.models import Q
from mcp import ClientSession, StdioServerParameters
from mcp.client.sse import sse_client
from mcp.client.stdio import stdio_client
from mcp.types import CallToolResult

from .credential_injector import CredentialInjector, ResolvedCredentials, _coerce_user_id
from .models import MCPServer
from .tool_cache import MCPToolCache

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Connection pool
# ---------------------------------------------------------------------------

SESSION_TTL: float = 300.0  # seconds a session stays alive without activity

_PoolKey = tuple[int, int | None]  # (server_id, user_id)


@dataclass
class _PoolEntry:
    stack: contextlib.AsyncExitStack
    session: ClientSession
    expires_at: float
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def expired(self) -> bool:
        return time.monotonic() >= self.expires_at

    def refresh(self) -> None:
        self.expires_at = time.monotonic() + SESSION_TTL


# Keyed by (server_id, user_id).  Entries are created lazily.
_pool: dict[_PoolKey, _PoolEntry] = {}
# One lock per key so only one coroutine creates/evicts an entry at a time.
_creation_locks: dict[_PoolKey, asyncio.Lock] = {}


def _creation_lock(key: _PoolKey) -> asyncio.Lock:
    if key not in _creation_locks:
        _creation_locks[key] = asyncio.Lock()
    return _creation_locks[key]


async def _evict(key: _PoolKey) -> None:
    """Close and remove a pool entry; safe to call when it doesn't exist."""
    entry = _pool.pop(key, None)
    if entry is not None:
        try:
            await entry.stack.aclose()
        except Exception:
            pass


class MCPClientManager:
    """Connect to a single MCP server on behalf of a user."""

    def __init__(self, server_id: int, user=None):
        self.server_id = server_id
        self.user = user

    async def get_server_config(self) -> MCPServer:
        server = await sync_to_async(_get_visible_server_sync)(
            self.server_id,
            _coerce_user_id(self.user),
            True,
        )
        if server is None:
            raise PermissionDenied("MCP server is not available for this user.")
        return server

    async def _resolve_credentials(self, server: MCPServer) -> ResolvedCredentials:
        return await CredentialInjector.resolve(server, self.user)

    @asynccontextmanager
    async def connect(self):
        """
        Async context manager yielding an initialised `ClientSession`.

        Sessions are pooled per (server_id, user_id) with SESSION_TTL expiry.
        The entry's asyncio.Lock is held for the duration of the `async with`
        block so concurrent callers are serialised (MCP sessions are not
        concurrency-safe).  If the session raises during use it is evicted
        so the next caller gets a fresh connection.
        """
        server = await self.get_server_config()
        resolved = await self._resolve_credentials(server)
        user_id = _coerce_user_id(self.user)
        key: _PoolKey = (self.server_id, user_id)

        # Ensure a session exists (or replace an expired one)
        async with _creation_lock(key):
            entry = _pool.get(key)
            if entry is None or entry.expired():
                await _evict(key)
                stack = contextlib.AsyncExitStack()
                try:
                    if server.type == "stdio":
                        session = await stack.enter_async_context(
                            self._connect_stdio(server, resolved)
                        )
                    elif server.type == "sse":
                        session = await stack.enter_async_context(
                            self._connect_sse(server, resolved)
                        )
                    else:
                        await stack.aclose()
                        raise ValueError(f"Unsupported MCP server type: {server.type}")
                    entry = _PoolEntry(
                        stack=stack,
                        session=session,
                        expires_at=time.monotonic() + SESSION_TTL,
                    )
                    _pool[key] = entry
                except Exception:
                    await stack.aclose()
                    raise

        # Serialise access: hold the entry lock for the entire call
        async with entry.lock:
            # Re-check expiry — another coroutine may have evicted while we waited
            if key not in _pool or _pool[key] is not entry:
                raise RuntimeError("MCP session was evicted; please retry")
            try:
                entry.refresh()
                yield entry.session
            except Exception:
                await _evict(key)
                raise

    @asynccontextmanager
    async def _connect_stdio(self, server: MCPServer, resolved: ResolvedCredentials):
        command = server.command
        if not command:
            raise ValueError(f"MCP server '{server.name}' is stdio but has no command")

        if not os.path.isabs(command):
            command = shutil.which(command) or command

        merged_env = {**os.environ, **(server.env or {}), **resolved.env_vars}
        params = StdioServerParameters(command=command, args=server.args or [], env=merged_env)

        try:
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    yield session
        except Exception:
            logger.exception("Failed stdio connection to MCP server %s", server.name)
            raise

    @asynccontextmanager
    async def _connect_sse(self, server: MCPServer, resolved: ResolvedCredentials):
        if not server.url:
            raise ValueError(f"MCP server '{server.name}' is SSE but has no URL")

        kwargs: dict[str, Any] = {}
        if resolved.headers:
            # Newer versions of the mcp SDK accept a `headers=` kwarg; fall back
            # silently if this version doesn't, rather than crashing.
            sig = inspect.signature(sse_client)
            if "headers" in sig.parameters:
                kwargs["headers"] = resolved.headers
            else:
                logger.warning(
                    "sse_client in this mcp version does not accept headers; "
                    "auth headers for server %s will not be sent.",
                    server.name,
                )

        try:
            async with sse_client(server.url, **kwargs) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    yield session
        except Exception:
            logger.exception("Failed SSE connection to MCP server %s", server.name)
            raise

    async def list_tools(self, use_cache: bool = True) -> list[dict[str, Any]]:
        """
        Return tool descriptors for this server.

        Cached (Redis) by default with a short TTL; pass `use_cache=False`
        to force a live fetch (used by the tool-cache invalidation path
        and by debug endpoints).
        """
        server = await self.get_server_config()
        resolved = await self._resolve_credentials(server)
        user_id = _coerce_user_id(self.user)

        if use_cache:
            cached = await MCPToolCache.get(self.server_id, user_id)
            if cached is not None:
                return cached

        async with self.connect() as session:
            result = await session.list_tools()
            tools = [
                {
                    "name": t.name,
                    "description": t.description or "",
                    "inputSchema": t.inputSchema or {"type": "object", "properties": {}},
                }
                for t in result.tools
            ]

        await MCPToolCache.set(self.server_id, user_id, tools)
        return tools

    async def call_tool(self, tool_name: str, arguments: dict[str, Any] | None = None) -> Any:
        """Execute a single tool and return a JSON-friendly payload."""
        async with self.connect() as session:
            result: CallToolResult = await session.call_tool(tool_name, arguments or {})

        if result.isError:
            raise RuntimeError(f"MCP tool '{tool_name}' reported error: {result}")

        return _serialise_tool_result(result)



def _serialise_tool_result(result: CallToolResult) -> Any:
    """Translate MCP CallToolResult content blocks into JSON-safe Python."""
    parts: list[Any] = []
    for content in result.content:
        ctype = getattr(content, "type", None)
        if ctype == "text":
            parts.append(content.text)
        elif ctype == "image":
            parts.append({
                "type": "image",
                "mime_type": getattr(content, "mimeType", None),
                "data": getattr(content, "data", None),
            })
        elif ctype == "resource":
            res = getattr(content, "resource", content)
            parts.append({
                "type": "resource",
                "uri": getattr(res, "uri", None),
                "mime_type": getattr(res, "mimeType", None),
                "text": getattr(res, "text", None),
            })
        else:
            parts.append(str(content))

    if len(parts) == 1:
        return parts[0]
    return parts


async def drain_pool() -> None:
    """
    Close all pooled sessions and clear the pool.

    Call this on process shutdown (e.g. Django AppConfig.ready teardown or
    a test fixture) to cleanly terminate stdio subprocesses and SSE streams.
    """
    keys = list(_pool.keys())
    for key in keys:
        await _evict(key)
    _creation_locks.clear()


def _visible_servers_queryset(user_id: int | None, enabled_only: bool = True):
    qs = MCPServer.objects.all()
    if enabled_only:
        qs = qs.filter(enabled=True)
    if user_id is None:
        return qs.filter(user__isnull=True)
    return qs.filter(Q(user__isnull=True) | Q(user_id=user_id))


def _get_visible_server_sync(server_id: int, user_id: int | None, enabled_only: bool = True) -> MCPServer | None:
    return _visible_servers_queryset(user_id, enabled_only).filter(id=server_id).first()


def _servers_for_user_sync(user_id: int | None):
    """Servers visible to this user (their own + system-wide)."""
    return list(_visible_servers_queryset(user_id, enabled_only=True))


async def get_servers_for_user(user) -> list[MCPServer]:
    """Return enabled MCPServer rows visible to the given user or user_id."""
    return await sync_to_async(_servers_for_user_sync)(_coerce_user_id(user))


async def get_visible_server_for_user(
    server_id: int,
    user,
    *,
    enabled_only: bool = True,
) -> MCPServer | None:
    """Return a visible MCPServer row, or None if missing/inaccessible."""
    return await sync_to_async(_get_visible_server_sync)(
        server_id,
        _coerce_user_id(user),
        enabled_only,
    )


async def get_all_tools_from_all_servers(user) -> list[dict[str, Any]]:
    """Aggregate tools from every server visible to `user`, with origin tags."""
    servers = await get_servers_for_user(user)
    tools: list[dict[str, Any]] = []
    for server in servers:
        try:
            manager = MCPClientManager(server.id, user=user)
            for t in await manager.list_tools():
                tools.append({
                    **t,
                    "server_id": server.id,
                    "server_name": server.name,
                })
        except Exception as e:  # noqa: BLE001
            logger.warning("Could not list tools for MCP server %s: %s", server.name, e)
    return tools
