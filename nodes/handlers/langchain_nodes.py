from typing import Any, Dict, List
from pydantic import Field

from nodes.handlers.base import BaseNodeHandler, NodeConfig, FieldType, FieldConfig
from compiler.schemas import ExecutionContext
from langchain_community.tools import WikipediaQueryRun
from langchain_community.utilities import WikipediaAPIWrapper

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

    async def execute(self, input_data: Dict[str, Any], config: Dict[str, Any], context: ExecutionContext) -> Any:
        tool_name = config.get("tool_name")
        query = config.get("query")
        
        # Support dynamic input via {{ }} if passed (resolved by runner before this)
        # But if the user wants to use upstream data, they can map it.
        # Here we assume 'query' might need resolution if it wasn't already handled by the generic runner.
        # The generic runner usually resolves {{ }} before calling execute.
        
        if tool_name not in TOOLS:
            raise ValueError(f"Unknown tool: {tool_name}")
            
        tool = TOOLS[tool_name]
        
        # Tools usually block, so we might need run_in_executor if they aren't async native
        # valid run methods: invoke, run
        try:
             # Most LC tools are sync compatible. run() is the standard entry point.
             # async_run might be available
             import asyncio
             if hasattr(tool, "arun"):
                 result = await tool.arun(query)
             else:
                 # Run sync tool in thread pool
                 result = await asyncio.to_thread(tool.run, query)
                 
             return {"result": result}
        except Exception as e:
            raise Exception(f"Tool execution failed: {str(e)}")

