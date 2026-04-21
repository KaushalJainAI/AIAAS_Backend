"""
MCPToolProvider — bridges MCP servers into the platform's agent tool loops.

This is the single integration surface used by:
    * the chat agent  (chat/graph.py, chat/tools.py)
    * the King orchestrator (executor/king.py), via MCPToolNode
    * the "buddy" help agent (reuses the chat agent tool list)

Tool names are namespaced so MCP tools never collide with built-in tools:

    mcp__<server_id>__<tool_name>

The provider exposes two calls:
    * `get_openai_tool_descriptors(user)` -> list of OpenAI-format function
      specs ready to merge into `AVAILABLE_TOOLS`.
    * `execute(name, arguments, user)` -> JSON-serialisable result.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from hashlib import sha1
from typing import Any

from django.core.exceptions import PermissionDenied

from .client import MCPClientManager, get_servers_for_user
from .credential_injector import CredentialInvalidError, CredentialMissingError
from .models import MCPServer

logger = logging.getLogger(__name__)

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
    async def get_openai_tool_descriptors(user) -> list[dict[str, Any]]:
        """
        Return OpenAI-format tool descriptors for every MCP tool visible to
        `user`. Safe to call on every chat turn — `list_tools` is cached.
        """
        servers = await get_servers_for_user(user)
        descriptors: list[dict[str, Any]] = []
        for server in servers:
            try:
                manager = MCPClientManager(server.id, user=user)
                tools = await manager.list_tools()
            except CredentialMissingError as e:
                # Don't advertise a tool the user can't actually call.
                logger.info("Skipping MCP server %s for user %s: %s", server.name, getattr(user, "id", None), e)
                continue
            except Exception as e:  # noqa: BLE001
                logger.warning("Failed to list tools for MCP server %s: %s", server.name, e)
                continue
            for t in tools:
                descriptors.append(_build_openai_descriptor(server, t))
        return descriptors

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
