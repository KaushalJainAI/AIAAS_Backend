from typing import Any, Dict, List
from pydantic import Field

from nodes.handlers.base import BaseNodeHandler
from compiler.schemas import ExecutionContext
from .client import MCPClientManager

class MCPToolNode(BaseNodeHandler):
    """
    Generic Node to execute an MCP Tool.
    """
    node_type = "mcp_tool"

    async def execute(self, input_data: Dict[str, Any], config: Dict[str, Any], context: ExecutionContext) -> Any:
        server_id = config.get("server_id")
        tool_name = config.get("tool_name")
        arguments = config.get("arguments", {})
        
        # Merge input_data into arguments if configured to do so
        # Assuming simple mapping: input_data IS the arguments, or overrides them
        # Strategy: Use arguments from config as base, update with input_data
        final_args = arguments.copy()
        final_args.update(input_data)
        
        if not server_id or not tool_name:
             raise ValueError("Configuration missing server_id or tool_name")

        manager = MCPClientManager(server_id)
        result = await manager.call_tool(tool_name, final_args)
        
        return result

    def validate_config(self, config: Dict[str, Any]) -> List[str]:
        errors = []
        if "server_id" not in config:
            errors.append("MCP Server ID is required")
        if "tool_name" not in config:
             errors.append("Tool Name is required")
        return errors

# We also need a schema generator that knows how to fetch tools dynamically?
# Standard Schema generation is static.
# Frontend usually fetches dynamic options.
# We will rely on Backend API to provide list of tools to frontend.
