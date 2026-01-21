"""
Core Node Handlers

Essential nodes for data manipulation, HTTP requests, and code execution.
"""
import json
import httpx
from datetime import datetime
from typing import Any, TYPE_CHECKING

from .base import (
    BaseNodeHandler,
    NodeCategory,
    FieldConfig,
    FieldType,
    HandleDef,
    NodeExecutionResult,
)

if TYPE_CHECKING:
    from compiler.schemas import ExecutionContext


class HTTPRequestNode(BaseNodeHandler):
    """
    Make HTTP requests to external APIs.
    
    Supports GET, POST, PUT, DELETE with headers and body.
    """
    
    node_type = "http_request"
    name = "HTTP Request"
    category = NodeCategory.ACTION.value
    description = "Make an HTTP request to an API"
    icon = "🌐"
    color = "#3b82f6"  # Blue
    
    fields = [
        FieldConfig(
            name="method",
            label="Method",
            field_type=FieldType.SELECT,
            options=["GET", "POST", "PUT", "PATCH", "DELETE"],
            default="GET"
        ),
        FieldConfig(
            name="url",
            label="URL",
            field_type=FieldType.STRING,
            placeholder="https://api.example.com/endpoint",
            description="Full URL to request"
        ),
        FieldConfig(
            name="headers",
            label="Headers",
            field_type=FieldType.JSON,
            required=False,
            default={},
            description="HTTP headers as JSON object"
        ),
        FieldConfig(
            name="body",
            label="Body",
            field_type=FieldType.JSON,
            required=False,
            default={},
            description="Request body (for POST/PUT/PATCH)"
        ),
        FieldConfig(
            name="timeout",
            label="Timeout (seconds)",
            field_type=FieldType.NUMBER,
            default=30,
            required=False
        ),
    ]
    
    outputs = [
        HandleDef(id="success", label="Success", handle_type="success"),
        HandleDef(id="error", label="Error", handle_type="error"),
    ]
    
    async def execute(
        self,
        input_data: dict[str, Any],
        config: dict[str, Any],
        context: 'ExecutionContext'
    ) -> NodeExecutionResult:
        method = config.get("method", "GET")
        url = config.get("url", "")
        headers = config.get("headers", {})
        body = config.get("body", {})
        timeout = config.get("timeout", 30)
        
        if not url:
            return NodeExecutionResult(
                success=False,
                error="URL is required",
                output_handle="error"
            )
        
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.request(
                    method=method,
                    url=url,
                    headers=headers,
                    json=body if method in ["POST", "PUT", "PATCH"] else None,
                )
                
                # Try to parse JSON response
                try:
                    response_data = response.json()
                except json.JSONDecodeError:
                    response_data = response.text
                
                return NodeExecutionResult(
                    success=True,
                    data={
                        "status_code": response.status_code,
                        "headers": dict(response.headers),
                        "body": response_data,
                        "url": str(response.url),
                    },
                    output_handle="success" if response.status_code < 400 else "error"
                )
                
        except httpx.TimeoutException:
            return NodeExecutionResult(
                success=False,
                error=f"Request timed out after {timeout}s",
                output_handle="error"
            )
        except Exception as e:
            return NodeExecutionResult(
                success=False,
                error=str(e),
                output_handle="error"
            )


class CodeNode(BaseNodeHandler):
    """
    Execute custom Python code.
    
    Receives input data, executes code, returns result.
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
            description="Python code to execute. Access input via 'data' variable.",
            placeholder='# Access input data via "data" variable\nresult = data.get("value", 0) * 2\nreturn {"doubled": result}'
        ),
    ]
    
    outputs = [
        HandleDef(id="success", label="Success", handle_type="success"),
        HandleDef(id="error", label="Error", handle_type="error"),
    ]
    
    async def execute(
        self,
        input_data: dict[str, Any],
        config: dict[str, Any],
        context: 'ExecutionContext'
    ) -> NodeExecutionResult:
        code = config.get("code", "")
        
        if not code:
            return NodeExecutionResult(
                success=False,
                error="No code provided",
                output_handle="error"
            )
        
        try:
            # Create safe execution environment
            local_vars = {
                "data": input_data,
                "context": {
                    "execution_id": str(context.execution_id),
                    "user_id": context.user_id,
                    "workflow_id": context.workflow_id,
                },
            }
            
            # Wrap code in function for return support
            wrapped_code = f"""
def __execute():
{chr(10).join('    ' + line for line in code.split(chr(10)))}
    return locals().get('result', {{}})
__result__ = __execute()
"""
            
            exec(wrapped_code, {"__builtins__": __builtins__}, local_vars)
            result = local_vars.get("__result__", {})
            
            return NodeExecutionResult(
                success=True,
                data=result if isinstance(result, dict) else {"result": result},
                output_handle="success"
            )
            
        except Exception as e:
            return NodeExecutionResult(
                success=False,
                error=f"Code execution failed: {str(e)}",
                output_handle="error"
            )


class SetNode(BaseNodeHandler):
    """
    Set/transform data fields.
    
    Create or modify data by setting field values.
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
        
        if keep_input:
            result = {**input_data, **values}
        else:
            result = values
        
        return NodeExecutionResult(success=True, data=result)


class IfNode(BaseNodeHandler):
    """
    Conditional branching.
    
    Routes execution based on a condition.
    """
    
    node_type = "if"
    name = "If"
    category = NodeCategory.CONDITIONAL.value
    description = "Branch based on condition"
    icon = "🔀"
    color = "#ec4899"  # Pink
    
    fields = [
        FieldConfig(
            name="field",
            label="Field to Check",
            field_type=FieldType.STRING,
            placeholder="data.status",
            description="Dot-notation path to field"
        ),
        FieldConfig(
            name="operator",
            label="Operator",
            field_type=FieldType.SELECT,
            options=["equals", "not_equals", "contains", "greater_than", "less_than", "is_empty", "is_not_empty"],
            default="equals"
        ),
        FieldConfig(
            name="value",
            label="Value",
            field_type=FieldType.STRING,
            required=False,
            description="Value to compare against"
        ),
    ]
    
    outputs = [
        HandleDef(id="true", label="True", handle_type="success"),
        HandleDef(id="false", label="False", handle_type="default"),
    ]
    
    async def execute(
        self,
        input_data: dict[str, Any],
        config: dict[str, Any],
        context: 'ExecutionContext'
    ) -> NodeExecutionResult:
        field_path = config.get("field", "")
        operator = config.get("operator", "equals")
        compare_value = config.get("value", "")
        
        # Get field value using dot notation
        field_value = input_data
        try:
            for key in field_path.split("."):
                field_value = field_value[key]
        except (KeyError, TypeError):
            field_value = None
        
        # Evaluate condition
        result = False
        
        if operator == "equals":
            result = str(field_value) == str(compare_value)
        elif operator == "not_equals":
            result = str(field_value) != str(compare_value)
        elif operator == "contains":
            result = str(compare_value) in str(field_value)
        elif operator == "greater_than":
            try:
                result = float(field_value) > float(compare_value)
            except (ValueError, TypeError):
                result = False
        elif operator == "less_than":
            try:
                result = float(field_value) < float(compare_value)
            except (ValueError, TypeError):
                result = False
        elif operator == "is_empty":
            result = field_value is None or field_value == "" or field_value == []
        elif operator == "is_not_empty":
            result = field_value is not None and field_value != "" and field_value != []
        
        return NodeExecutionResult(
            success=True,
            data=input_data,
            output_handle="true" if result else "false"
        )
