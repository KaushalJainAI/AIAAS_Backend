from typing import Any, Dict, TYPE_CHECKING

from nodes.handlers.base import (
    BaseNodeHandler,
    FieldType,
    FieldConfig,
    HandleDef,
    NodeExecutionResult,
)
from langchain_community.tools import WikipediaQueryRun
from langchain_community.utilities import WikipediaAPIWrapper

if TYPE_CHECKING:
    from compiler.schemas import ExecutionContext

# We can add more tools here or dynamic loading
TOOLS = {
    "wikipedia": WikipediaQueryRun(api_wrapper=WikipediaAPIWrapper())
}


class LangChainToolNode(BaseNodeHandler):
    """
    Node that wraps standard LangChain tools.
    """
    node_type = "langchain_tool"
    name = "LangChain Tool"
    category = "integration"
    description = "Execute a standard LangChain tool"
    icon = "🔗"
    color = "#10b981"
    
    fields = [
        FieldConfig(
            name="tool_name", 
            label="Tool", 
            field_type=FieldType.SELECT, 
            options=list(TOOLS.keys()),
            required=True
        ),
        FieldConfig(
            name="query", 
            label="Query / Input", 
            field_type=FieldType.STRING, 
            description="Input for the tool",
            required=True
        )
    ]
    
    outputs = [
        HandleDef(id="output", label="Output"),
        HandleDef(id="error", label="Error", handle_type="error"),
    ]

    async def execute(
        self,
        input_data: Dict[str, Any],
        config: Dict[str, Any],
        context: 'ExecutionContext'
    ) -> NodeExecutionResult:
        tool_name = config.get("tool_name")
        query = config.get("query")
        
        if tool_name not in TOOLS:
            return NodeExecutionResult(
                success=False,
                error=f"Unknown tool: {tool_name}",
                output_handle="error"
            )
            
        tool = TOOLS[tool_name]
        
        try:
            import asyncio
            if hasattr(tool, "arun"):
                result = await tool.arun(query)
            else:
                # Run sync tool in thread pool
                result = await asyncio.to_thread(tool.run, query)
                 
            return NodeExecutionResult(
                success=True,
                data={"result": result},
                output_handle="output"
            )
        except Exception as e:
            return NodeExecutionResult(
                success=False,
                error=f"Tool execution failed: {str(e)}",
                output_handle="error"
            )

