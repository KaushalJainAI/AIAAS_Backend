"""
MCPClientManager — opens ephemeral connections to an MCP server and exposes
`list_tools` / `call_tool`.

Credential injection (env vars for stdio, headers for SSE) is handled by
`CredentialInjector`. Tool listing is cached via `MCPToolCache`. Neither
concern lives in this module.
"""
from __future__ import annotations

import inspect
import logging
import os
import shutil
from contextlib import asynccontextmanager
from typing import Any

from asgiref.sync import sync_to_async
from mcp import ClientSession, StdioServerParameters
from mcp.client.sse import sse_client
from mcp.client.stdio import stdio_client
from mcp.types import CallToolResult

from .credential_injector import CredentialInjector, ResolvedCredentials
from .models import MCPServer
from .tool_cache import MCPToolCache

logger = logging.getLogger(__name__)


class MCPClientManager:
    """Connect to a single MCP server on behalf of a user."""

    def __init__(self, server_id: int, user=None):
        self.server_id = server_id
        self.user = user

    async def get_server_config(self) -> MCPServer:
        return await MCPServer.objects.aget(id=self.server_id)

    async def _resolve_credentials(self, server: MCPServer) -> ResolvedCredentials:
        return await CredentialInjector.resolve(server, self.user)

    @asynccontextmanager
    async def connect(self):
        """Async context manager yielding an initialised `ClientSession`."""
        server = await self.get_server_config()
        resolved = await self._resolve_credentials(server)

        if server.type == "stdio":
            async with self._connect_stdio(server, resolved) as session:
                yield session
        elif server.type == "sse":
            async with self._connect_sse(server, resolved) as session:
                yield session
        else:
            raise ValueError(f"Unsupported MCP server type: {server.type}")

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
        if use_cache:
            cached = await MCPToolCache.get(self.server_id)
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

        await MCPToolCache.set(self.server_id, tools)
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


def _servers_for_user_sync(user_id: int | None):
    """Servers visible to this user (their own + system-wide)."""
    from django.db.models import Q

    qs = MCPServer.objects.filter(enabled=True)
    if user_id is None:
        qs = qs.filter(user__isnull=True)
    else:
        qs = qs.filter(Q(user__isnull=True) | Q(user_id=user_id))
    return list(qs)


async def get_servers_for_user(user) -> list[MCPServer]:
    """Return enabled MCPServer rows visible to the given user or user_id."""
    from .credential_injector import _coerce_user_id

    return await sync_to_async(_servers_for_user_sync)(_coerce_user_id(user))


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
