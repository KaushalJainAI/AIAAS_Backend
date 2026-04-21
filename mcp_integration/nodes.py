"""
MCPToolNode — workflow node that invokes a tool on an MCP server.

Dynamic configuration (which MCP server, which tool) is supplied by the
frontend. Credential injection, server resolution, and result normalisation
all live in their own modules and are imported here — this file stays thin.
"""
from __future__ import annotations

import logging
from typing import Any

from compiler.schemas import ExecutionContext
from nodes.handlers.base import (
    BaseNodeHandler,
    FieldConfig,
    FieldType,
    HandleDef,
    NodeCategory,
    NodeExecutionResult,
)

from .client import MCPClientManager
from .credential_injector import CredentialInvalidError, CredentialMissingError

logger = logging.getLogger(__name__)


class MCPToolNode(BaseNodeHandler):
    """Invoke a single MCP tool during workflow execution."""

    node_type = "mcp_tool"
    name = "MCP Tool"
    category = NodeCategory.INTEGRATION.value
    description = "Call a tool exposed by a configured MCP server."
    icon = "🔌"
    color = "#7c3aed"

    fields = [
        FieldConfig(
            name="server_id",
            label="MCP Server",
            field_type=FieldType.SELECT,
            required=True,
            description="The MCP server that exposes the tool.",
        ),
        FieldConfig(
            name="tool_name",
            label="Tool",
            field_type=FieldType.SELECT,
            required=True,
            description="Which tool on the selected server to invoke.",
        ),
        FieldConfig(
            name="arguments",
            label="Arguments (JSON)",
            field_type=FieldType.JSON,
            required=False,
            default={},
            description="Static arguments. Merged with (and overridden by) upstream node data.",
        ),
    ]
    inputs = [HandleDef(id="input")]
    outputs = [HandleDef(id="output")]

    async def execute(
        self,
        input_data: dict[str, Any],
        config: dict[str, Any],
        context: ExecutionContext,
    ) -> NodeExecutionResult:
        server_id = config.get("server_id")
        tool_name = config.get("tool_name")
        static_args = config.get("arguments") or {}

        if not server_id or not tool_name:
            return NodeExecutionResult(
                success=False,
                error="MCPToolNode requires both 'server_id' and 'tool_name' in config.",
            )

        merged_args: dict[str, Any] = {}
        if isinstance(static_args, dict):
            merged_args.update(static_args)
        if isinstance(input_data, dict):
            merged_args.update(input_data)

        manager = MCPClientManager(int(server_id), user=context.user_id)
        try:
            result = await manager.call_tool(tool_name, merged_args)
        except CredentialMissingError as e:
            return NodeExecutionResult(success=False, error=str(e))
        except CredentialInvalidError as e:
            return NodeExecutionResult(success=False, error=str(e))
        except Exception as e:  # noqa: BLE001
            logger.exception("MCPToolNode failed: server=%s tool=%s", server_id, tool_name)
            return NodeExecutionResult(success=False, error=f"MCP tool '{tool_name}' failed: {e}")

        payload = result if isinstance(result, dict) else {"result": result}
        return NodeExecutionResult.from_data(payload)

    def validate_config(self, config: dict[str, Any]) -> list[str]:
        errors: list[str] = []
        if not config.get("server_id"):
            errors.append("MCP Server is required")
        if not config.get("tool_name"):
            errors.append("Tool name is required")
        return errors
