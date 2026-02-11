import asyncio
import os
import shutil
import logging
from typing import Any, List, Dict
from contextlib import asynccontextmanager

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.sse import sse_client
from mcp.types import Tool, CallToolResult

from .models import MCPServer

logger = logging.getLogger(__name__)

class MCPClientManager:
    """
    Manages connections to MCP servers.
    Handles ephemeral connections for listing tools and executing requests.
    """
    
    def __init__(self, server_id: int):
        self.server_id = server_id
        
    async def get_server_config(self) -> MCPServer:
        """Fetch server config from DB asynchronously."""
        return await MCPServer.objects.aget(id=self.server_id)

    @asynccontextmanager
    async def connect(self):
        """
        Async context manager that yields a connected ClientSession.
        Handles connection setup and teardown.
        """
        server = await self.get_server_config()
        
        if server.type == 'stdio':
            # Resolve executable path (e.g. 'npx' -> '/usr/bin/npx')
            command = server.command
            if not command:
                 raise ValueError("Server command is required for stdio type")
            
            # Simple path resolution if not absolute
            if not os.path.isabs(command):
                command = shutil.which(command) or command

            server_params = StdioServerParameters(
                command=command,
                args=server.args,
                env={**os.environ, **server.env} # Merge with system env
            )
            
            try:
                async with stdio_client(server_params) as (read, write):
                    async with ClientSession(read, write) as session:
                        yield session
            except Exception as e:
                logger.error(f"Failed to connect to stdio server {server.name}: {e}")
                raise

        elif server.type == 'sse':
             if not server.url:
                 raise ValueError("Server URL is required for SSE type")
                 
             try:
                 # TODO: Add headers/auth support if needed
                 async with sse_client(server.url) as (read, write):
                     async with ClientSession(read, write) as session:
                         yield session
             except Exception as e:
                 logger.error(f"Failed to connect to SSE server {server.name}: {e}")
                 raise
        else:
            raise ValueError(f"Unsupported server type: {server.type}")

    async def list_tools(self) -> List[Dict[str, Any]]:
        """List available tools from the server."""
        async with self.connect() as session:
            await session.initialize()
            result = await session.list_tools()
            # Convert mcp.types.Tool objects to dicts
            return [
                {
                    "name": tool.name, 
                    "description": tool.description, 
                    "inputSchema": tool.inputSchema
                } 
                for tool in result.tools
            ]

    async def call_tool(self, tool_name: str, arguments: Dict[str, Any] = None) -> Any:
        """Execute a tool."""
        async with self.connect() as session:
            await session.initialize()
            result: CallToolResult = await session.call_tool(tool_name, arguments or {})
            
            # Process result
            if result.isError:
                raise Exception(f"Tool execution error: {result}")
            
            # result.content is a list of Content (TextContent, ImageContent, etc.)
            # We squash it into a friendly format for the workflow engine
            output_data = []
            for content in result.content:
                if content.type == 'text':
                    output_data.append(content.text)
                elif content.type == 'image':
                    output_data.append(f"[Image: {content.mimeType}]")
                elif content.type == 'resource':
                    output_data.append(f"[Resource: {content.uri}]")
            
            if len(output_data) == 1:
                return output_data[0]
            return output_data

async def get_all_tools_from_all_servers(user_id: int | None = None) -> List[Dict[str, Any]]:
    """Helper to aggregate tools from all active servers visible to a user.
    
    Returns tools from:
    - System-wide servers (user=NULL)
    - Servers owned by the specified user
    """
    from django.db.models import Q
    
    qs = MCPServer.objects.filter(enabled=True)
    if user_id is not None:
        qs = qs.filter(Q(user__isnull=True) | Q(user_id=user_id))
    
    tools = []
    async for server in qs:
        try:
            manager = MCPClientManager(server.id)
            server_tools = await manager.list_tools()
            for t in server_tools:
                t['server_id'] = server.id
                t['server_name'] = server.name
            tools.extend(server_tools)
        except Exception as e:
            logger.warning(f"Could not list tools for server {server.name}: {e}")
    return tools
