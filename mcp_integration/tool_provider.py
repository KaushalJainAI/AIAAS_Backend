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
from typing import Any

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
    safe = _SAFE_TOOL_NAME_RE.sub("_", tool_name)
    name = f"{TOOL_PREFIX}{server_id}__{safe}"
    if len(name) > MAX_NAME_LEN:
        # Keep the server_id prefix and truncate the tool portion.
        keep = MAX_NAME_LEN - len(f"{TOOL_PREFIX}{server_id}__")
        name = f"{TOOL_PREFIX}{server_id}__{safe[:keep]}"
    return name


def decode_tool_name(name: str) -> tuple[int, str] | None:
    """Reverse of `encode_tool_name`. Returns None if not an MCP tool name."""
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
    async def execute(name: str, arguments: dict[str, Any] | None, user) -> str:
        """
        Execute a namespaced MCP tool. Returns a string (JSON-encoded for
        structured payloads) so it plugs directly into the chat tool loop,
        which expects `str` results.
        """
        decoded = decode_tool_name(name)
        if decoded is None:
            return f"Error: '{name}' is not a valid MCP tool name."
        server_id, tool_name = decoded

        try:
            manager = MCPClientManager(server_id, user=user)
            result = await manager.call_tool(tool_name, arguments or {})
        except CredentialMissingError as e:
            return json.dumps({"error": str(e), "code": "credential_missing"})
        except CredentialInvalidError as e:
            return json.dumps({"error": str(e), "code": "credential_invalid"})
        except Exception as e:  # noqa: BLE001
            logger.exception("MCP tool %s failed", name)
            return json.dumps({"error": f"MCP tool '{tool_name}' failed: {e}", "code": "tool_error"})

        if isinstance(result, str):
            return result
        try:
            return json.dumps(result)
        except TypeError:
            return str(result)
