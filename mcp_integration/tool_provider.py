"""
MCPToolProvider — bridges MCP servers into the platform's agent tool loops.

This is the single integration surface used by the chat agent and, through it,
by agent runs (`chat/tools/`, dispatched via the `mcp` grant in
`agents/agent/runtime.py::GRANT_TOOLS`). It is now the only such surface:
the King orchestrator reached MCP through `MCPToolNode`, and both went with
the DAG runtime.

Tool names are namespaced so MCP tools never collide with built-in tools:

    mcp__<server_id>__<tool_name>

The provider exposes two calls:
    * `get_openai_tool_descriptors(user, server_ids=None)` -> list of
      OpenAI-format function specs ready to merge into `AVAILABLE_TOOLS`,
      optionally narrowed to a chosen few connections.
    * `execute(name, arguments, user)` -> JSON-serialisable result.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from hashlib import sha1
from collections.abc import Callable, Iterable
from typing import Any

from django.core.exceptions import PermissionDenied

from .client import MCPClientManager, get_servers_for_user, AGENT_LIST_TOOLS_TIMEOUT
from .credential_injector import (
    CredentialInvalidError,
    CredentialMissingError,
    _coerce_user_id,
)
from .models import MCPServer

import os

logger = logging.getLogger(__name__)

# Emergency brake, read at call time so tests can flip it without reload.
# `MCP_DISABLED=True` makes every listing path return no tools without
# spawning anything: the turn still answers, just without connector tools.
# For when a wedged subprocess pool is taking the whole box down and the
# right fix (disable unused connections, redeploy) needs a minute.
def _mcp_disabled() -> bool:
    return os.environ.get("MCP_DISABLED", "False").lower() in ("true", "1", "yes")

TOOL_PREFIX = "mcp__"
_NAME_RE = re.compile(r"^mcp__(\d+)__(.+)$")
# OpenAI/Anthropic function names: [a-zA-Z0-9_-]{1,64}
_SAFE_TOOL_NAME_RE = re.compile(r"[^a-zA-Z0-9_\-]")
MAX_NAME_LEN = 64


def encode_tool_name(server_id: int, tool_name: str) -> str:
    """Produce a namespaced, schema-safe tool name."""
    safe = _SAFE_TOOL_NAME_RE.sub("_", tool_name).strip("_") or "tool"
    digest = sha1(tool_name.encode("utf-8")).hexdigest()[:8]
    prefix = f"{TOOL_PREFIX}{server_id}__"
    suffix_len = 9  # "_" + 8-char digest
    keep = max(1, MAX_NAME_LEN - len(prefix) - suffix_len)
    return f"{prefix}{safe[:keep]}_{digest}"


def decode_tool_name(name: str) -> tuple[int, str] | None:
    """Return the server id and encoded tool suffix for an MCP tool name."""
    m = _NAME_RE.match(name)
    if not m:
        return None
    return int(m.group(1)), m.group(2)


def is_mcp_tool(name: str) -> bool:
    return name.startswith(TOOL_PREFIX)


@dataclass
class _ToolBinding:
    """Resolved binding from a namespaced name back to (server, actual_tool_name)."""
    server_id: int
    server_name: str
    original_tool_name: str


def _build_openai_descriptor(server: MCPServer, tool: dict[str, Any]) -> dict[str, Any]:
    tool_name = tool.get("name", "")
    encoded = encode_tool_name(server.id, tool_name)
    description = tool.get("description") or f"{tool_name} (from MCP server '{server.name}')"
    schema = tool.get("inputSchema") or {"type": "object", "properties": {}}
    # OpenAI requires top-level "type": "object" for parameters.
    if not isinstance(schema, dict) or schema.get("type") != "object":
        schema = {"type": "object", "properties": {}}
    return {
        "type": "function",
        "function": {
            "name": encoded,
            "description": f"[{server.name}] {description}",
            "parameters": schema,
        },
    }


class MCPToolProvider:
    """Stateless facade. All methods take `user` explicitly — no hidden state."""

    @staticmethod
    async def get_openai_tool_descriptors(
        user, server_ids: Iterable[int] | None = None,
        tool_filter: Callable[[int, str], bool] | None = None,
    ) -> list[dict[str, Any]]:
        """
        Return OpenAI-format tool descriptors for every MCP tool visible to
        `user`. Safe to call on every chat turn — `list_tools` is cached.

        `server_ids` narrows that to a chosen few connections. `None` means
        "every one the user has", which is what chat passes: a human is typing
        and watching, so the whole workspace is the right scope. An agent run
        passes its own selection, because an agent is configuration that runs
        unattended and "every connection this account owns" is not a blast
        radius anyone chose.

        Filtering here rather than in the caller keeps the narrowing on the
        same side as `get_servers_for_user`, so a server the user has switched
        off on Connections is excluded before the selection is even consulted —
        a stale id in an agent's config can only ever take tools away.

        `tool_filter(server_id, original_name)` narrows one step further, to
        individual tools. It runs here because this is the only place that holds
        a tool's *original* name: everything downstream sees the encoded form,
        whose middle section has already been through `_SAFE_TOOL_NAME_RE`. Chat
        passes None — a human is typing and watching.
        """
        if _mcp_disabled():
            return []
        servers = await get_servers_for_user(user)
        if server_ids is not None:
            allowed = set(server_ids)
            servers = [s for s in servers if s.id in allowed]

        def _keep(server_id: int, tool: dict[str, Any]) -> bool:
            return tool_filter is None or tool_filter(server_id, tool.get('name') or '')

        async def _descriptors_for(server) -> list[dict[str, Any]]:
            # Bound each server: a hung/absent stdio server must not stall the
            # whole agent turn. All servers are queried concurrently under one
            # shared 8s budget each — deliberately *unbounded*: batching them
            # through a semaphore turns one 8s timeout into ceil(n/k)*8s of
            # dead air when all of them are cold, which is exactly the hang
            # this path must not produce.
            try:
                manager = MCPClientManager(server.id, user=user)
                tools = await asyncio.wait_for(manager.list_tools(), timeout=AGENT_LIST_TOOLS_TIMEOUT)
            except CredentialMissingError as e:
                # Don't advertise a tool the user can't actually call.
                logger.info("Skipping MCP server %s for user %s: %s", server.name, getattr(user, "id", None), e)
                return []
            except (asyncio.TimeoutError, TimeoutError):
                # The 8s agent budget is far below a cold `npx` start
                # (~21s), so a cold connector times out here by design and
                # returns no tools for this turn. Two things keep that from
                # becoming every turn: remember the failure so the next
                # turn backs off for FAILURE_TTL instead of paying another
                # 8s immediately, and warm the cache behind the turn with
                # the full 25s+15s budget so the retry hits instead of
                # re-dialling cold. Without both, a connector whose cache
                # never filled costs every turn 8s for nothing.
                logger.warning("Timed out listing tools for MCP server %s", server.name)
                try:
                    from .client import _record_failure, _refresh_in_background
                    from .client import AGENT_LIST_TOOLS_TIMEOUT as _budget

                    _record_failure(
                        (server.id, _coerce_user_id(user)),
                        f"Timed out after {_budget:.0f}s listing tools for "
                        f"'{server.name}' (warming in background).",
                    )
                    _refresh_in_background(server.id, user)
                except Exception:  # noqa: BLE001 — backoff must never break listing
                    logger.debug("Failed to schedule MCP background warm", exc_info=True)
                return []
            except Exception as e:  # noqa: BLE001
                logger.warning("Failed to list tools for MCP server %s: %s", server.name, e)
                return []
            return [_build_openai_descriptor(server, t) for t in tools
                    if _keep(server.id, t)]

        results = await asyncio.gather(
            *(_descriptors_for(s) for s in servers), return_exceptions=True
        )
        return [
            d for group in results
            if not isinstance(group, BaseException)
            for d in group
        ]

    @staticmethod
    async def _resolve_binding(name: str, user) -> _ToolBinding | None:
        decoded = decode_tool_name(name)
        if decoded is None:
            return None
        server_id, _ = decoded
        manager = MCPClientManager(server_id, user=user)
        server = await manager.get_server_config()
        tools = await manager.list_tools()
        for tool in tools:
            original_name = tool.get("name", "")
            if encode_tool_name(server_id, original_name) == name:
                return _ToolBinding(
                    server_id=server_id,
                    server_name=server.name,
                    original_tool_name=original_name,
                )
        return None

    @staticmethod
    async def execute(name: str, arguments: dict[str, Any] | None, user) -> str:
        """
        Execute a namespaced MCP tool. Returns a string (JSON-encoded for
        structured payloads) so it plugs directly into the chat tool loop,
        which expects `str` results.
        """
        try:
            binding = await MCPToolProvider._resolve_binding(name, user)
            if binding is None:
                return json.dumps({"error": f"Unknown or unavailable MCP tool '{name}'.", "code": "tool_not_found"})
            manager = MCPClientManager(binding.server_id, user=user)
            result = await manager.call_tool(binding.original_tool_name, arguments or {})
        except PermissionDenied:
            return json.dumps({"error": f"Unknown or unavailable MCP tool '{name}'.", "code": "tool_not_found"})
        except CredentialMissingError as e:
            return json.dumps({"error": str(e), "code": "credential_missing"})
        except CredentialInvalidError as e:
            return json.dumps({"error": str(e), "code": "credential_invalid"})
        except Exception as e:  # noqa: BLE001
            logger.exception("MCP tool %s failed", name)
            return json.dumps({"error": f"MCP tool '{name}' failed: {e}", "code": "tool_error"})

        if isinstance(result, str):
            return result
        try:
            return json.dumps(result)
        except TypeError:
            return str(result)
