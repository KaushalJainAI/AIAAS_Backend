"""
Core Node Handlers

Essential nodes for data manipulation and code execution.

NOTE: HTTPRequestNode has been moved to integration_nodes.py for consistency.
"""
from typing import Any, TYPE_CHECKING
from nodes.handlers.base import (
    BaseNodeHandler,
    NodeCategory,
    FieldConfig,
    FieldType,
    HandleDef,
    NodeExecutionResult,
    NodeItem,
    NodeExecutionError,
)

if TYPE_CHECKING:
    from compiler.schemas import ExecutionContext


class CodeNode(BaseNodeHandler):
    """
    Execute custom Python code in a secure, high-performance sandbox.
    """
    
    node_type = "code"
    name = "Code"
    category = NodeCategory.TRANSFORM.value
    description = "Run custom Python code"
    icon = "💻"
    color = "#8b5cf6"  # Purple
    
    fields = [
        FieldConfig(
            name="code",
            label="Python Code",
            field_type=FieldType.CODE,
            description="Pure Python function body. Use 'item' to access data.",
            placeholder='return {"sum": item["a"] + item["b"]}'
        ),
    ]
    
    outputs = [
        HandleDef(id="output-0", label="Success", handle_type="success"),
    ]

    def _get_sandbox_globals(self):
        """Define a strict whitelist for in-process safety."""
        import math, json, datetime
        return {
            "__builtins__": {
                "len": len, "range": range, "min": min, "max": max,
                "sum": sum, "abs": abs, "str": str, "int": int,
                "float": float, "list": list, "dict": dict, "set": set,
                "bool": bool, "enumerate": enumerate, "zip": zip,
                "round": round, "any": any, "all": all, "sorted": sorted,
                "Exception": Exception, "ValueError": ValueError, "TypeError": TypeError,
            },
            "math": math,
            "json": json,
            "datetime": datetime,
            "NodeExecutionError": NodeExecutionError,
        }

    async def execute(
        self,
        input_data: dict[str, Any],
        config: dict[str, Any],
        context: 'ExecutionContext'
    ) -> NodeExecutionResult:
        user_code = config.get("code", "")
        if not user_code:
            return NodeExecutionResult(success=False, error="No code provided")

        # 1. Pre-Compile (Performance: ONCE per node execution)
        wrapped_code = "def __user_fn__(item, context, config):\n"
        wrapped_code += "\n".join(f"    {line}" for line in user_code.splitlines())
        
        try:
            code_obj = compile(wrapped_code, "<user_code>", "exec")
        except SyntaxError as e:
            return NodeExecutionResult(success=False, error=f"Syntax Error: {str(e)}")

        # 2. Setup Sandbox & Extract Function
        sandbox_globals = self._get_sandbox_globals()
        exec(code_obj, sandbox_globals)
        user_fn = sandbox_globals.get("__user_fn__")

        if not user_fn:
            return NodeExecutionResult(success=False, error="Failed to initialize user function")

        # 3. Execution Context Data
        context_data = {
            "execution_id": str(context.execution_id),
            "workflow_id": context.workflow_id,
        }

        # 4. Item Processing (Centralized in BaseNodeHandler)
        def run_in_sandbox(item_json):
            # Strict per-item isolation: No global mutation allowed
            result = user_fn(item_json, context_data, config)
            if not isinstance(result, dict):
                 raise ValueError(
                     f"Code Node Error: Expected a dictionary output (e.g. {{'key': 'value'}}), "
                     f"but got {type(result).__name__} ({result}).\n"
                     f"Please update your code to return a dictionary."
                 )
            return result

        try:
            import asyncio
            # Use current_input if available (for batching/looping), else wrap input_data
            raw_items = context.current_input if (hasattr(context, 'current_input') and context.current_input) else [NodeItem(json=input_data)]
            
            # Ensure all items are NodeItem objects (fix for dict attribute error)
            items = [NodeItem(json=i) if isinstance(i, dict) else i for i in raw_items]
            
            output_items = await asyncio.to_thread(self._process_items, items, run_in_sandbox, context)
            
            return NodeExecutionResult(
                success=True,
                items=output_items,
                output_handle="output-0"
            )
        except Exception as e:
            return NodeExecutionResult(success=False, error=str(e))


class SetNode(BaseNodeHandler):
    """
    Set/transform data fields using centralized item processing.
    """
    
    node_type = "set"
    name = "Set"
    category = NodeCategory.TRANSFORM.value
    description = "Set or transform data fields"
    icon = "✏️"
    color = "#f59e0b"  # Amber
    
    fields = [
        FieldConfig(
            name="values",
            label="Values",
            field_type=FieldType.JSON,
            description="Key-value pairs to set",
            default={}
        ),
        FieldConfig(
            name="keep_input",
            label="Keep Input Data",
            field_type=FieldType.BOOLEAN,
            default=True,
            description="Merge with input data instead of replacing"
        ),
    ]
    
    async def execute(
        self,
        input_data: dict[str, Any],
        config: dict[str, Any],
        context: 'ExecutionContext'
    ) -> NodeExecutionResult:
        values = config.get("values", {})
        keep_input = config.get("keep_input", True)
        
        def transform(item_json):
            if keep_input:
                return {**item_json, **values}
            return values

        raw_items = context.current_input if (hasattr(context, 'current_input') and context.current_input) else [NodeItem(json=input_data)]
        
        # Ensure all items are NodeItem objects
        items = [NodeItem(json=i) if isinstance(i, dict) else i for i in raw_items]
        
        output_items = self._process_items(items, transform, context)
        
        return NodeExecutionResult(success=True, items=output_items)
