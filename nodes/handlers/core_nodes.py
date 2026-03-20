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
            description="Write a function 'main(item, context)' or just use 'return' for simple logic.",
            default="def main(item, context):\n    # Access input via item['field']\n    # Return a dictionary\n    return {\"result\": \"success\"}"
        ),
    ]
    
    outputs = [
        HandleDef(id="output-0", label="Success", handle_type="success"),
    ]

    def _extract_code(self, text: str) -> str:
        """Utility to extract code from markdown for the user."""
        if not isinstance(text, str): return text
        import re
        # 1. ```python\ncode\n```
        match = re.search(r"```(?:python|py)?\n(.*?)\n```", text, re.DOTALL | re.IGNORECASE)
        if match:
            return match.group(1).strip()
        # 2. ```code```
        match = re.search(r"```(.*?)```", text, re.DOTALL)
        if match:
            return match.group(1).strip()
        return text.strip()

    def _get_sandbox_globals(self):
        """Define a strict whitelist for in-process safety."""
        import math, json, datetime
        return {
            "extract_code": self._extract_code,
            "__builtins__": {
                "len": len, "range": range, "min": min, "max": max,
                "sum": sum, "abs": abs, "str": str, "int": int,
                "float": float, "list": list, "dict": dict, "set": set,
                "bool": bool, "enumerate": enumerate, "zip": zip,
                "round": round, "any": any, "all": all, "sorted": sorted,
                "getattr": getattr, "hasattr": hasattr,
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
        user_code = config.get("code", "").strip()
        if not user_code:
            return NodeExecutionResult(success=False, error="No code provided")

        # Auto-extract if it looks like markdown (piped from LLM)
        user_code = self._extract_code(user_code)

        # 1. Prepare Sandbox
        sandbox_globals = self._get_sandbox_globals()
        
        # 2. Detection Logic: Full Function vs Body Only
        # If the user defines 'main(item, context)', we'll call that.
        # Otherwise, we wrap it in a function like before.
        
        is_full_function = "def main(" in user_code
        
        if is_full_function:
            try:
                # Execute the code to define functions
                code_obj = compile(user_code, "<user_code>", "exec")
                exec(code_obj, sandbox_globals)
                user_fn = sandbox_globals.get("main")
            except Exception as e:
                return NodeExecutionResult(success=False, error=f"Compilation/Execution Error: {str(e)}")
        else:
            # Body-only mode (legacy/simple)
            wrapped_code = "def __user_fn__(item, context, config):\n"
            wrapped_code += "\n".join(f"    {line}" for line in user_code.splitlines())
            try:
                code_obj = compile(wrapped_code, "<user_code>", "exec")
                exec(code_obj, sandbox_globals)
                user_fn = sandbox_globals.get("__user_fn__")
            except Exception as e:
                return NodeExecutionResult(success=False, error=f"Syntax Error: {str(e)}")

        if not user_fn:
            return NodeExecutionResult(success=False, error="Failed to find entry point 'main' or valid logic.")

        # 3. Execution Context Data
        context_data = {
            "execution_id": str(context.execution_id),
            "workflow_id": context.workflow_id,
        }

        # 4. Item Processing
        def run_in_sandbox(item_json):
            # If calling 'main', it's (item, context). If '__user_fn__', it's (item, context, config).
            if is_full_function:
                result = user_fn(item_json, context_data)
            else:
                result = user_fn(item_json, context_data, config)
                
            if not isinstance(result, dict):
                 raise ValueError(
                     f"Code Node Error: Expected a dictionary output (e.g. {{'key': 'value'}}), "
                     f"but got {type(result).__name__}.\n"
                     f"Please return a dictionary from your function."
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
