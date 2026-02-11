"""
Core Node Handlers

Essential nodes for data manipulation and code execution.

NOTE: HTTPRequestNode has been moved to integration_nodes.py for consistency.
"""
import json
from typing import Any, TYPE_CHECKING

from .base import (
    BaseNodeHandler,
    NodeCategory,
    FieldConfig,
    FieldType,
    HandleDef,
    NodeExecutionResult,
    NodeItem,
)

if TYPE_CHECKING:
    from compiler.schemas import ExecutionContext


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
        HandleDef(id="output-0", label="Success", handle_type="success"),
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
                output_handle="output-0"
            )
        
        # Normalize input to items format
        if isinstance(input_data, list):
            items = input_data
        elif isinstance(input_data, dict) and "json" in input_data:
            items = [input_data]
        else:
            items = [{"json": input_data}]
        
        try:
            output_items = []
            
            for idx, item in enumerate(items):
                item_data = item.get("json", item) if isinstance(item, dict) else {}
                
                # Create safe execution environment
                local_vars = {
                    "data": item_data,
                    "$json": item_data,  # n8n style access
                    "$item": item,
                    "$itemIndex": idx,
                    "context": {
                        "execution_id": str(context.execution_id),
                        "user_id": context.user_id,
                        "workflow_id": context.workflow_id,
                    },
                }
                
                # Wrap code in function for return support
                wrapped_code = f"""
def __execute(data, context):
{chr(10).join('    ' + line for line in code.split(chr(10)))}
    return locals().get('result', {{}})
__result__ = __execute(data, context)
"""
                
                exec(wrapped_code, {"__builtins__": __builtins__}, local_vars)
                result = local_vars.get("__result__", {})
                
                output_items.append(NodeItem(
                    json=result if isinstance(result, dict) else {"result": result},
                    pairedItem={"item": idx}
                ))
            
            return NodeExecutionResult(
                success=True,
                items=output_items,
                output_handle="output-0"
            )
            
        except Exception as e:
            return NodeExecutionResult(
                success=False,
                error=f"Code execution failed: {str(e)}",
                output_handle="output-0"
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
        
        # Normalize input to items format
        if isinstance(input_data, list):
            items = input_data
        elif isinstance(input_data, dict) and "json" in input_data:
            items = [input_data]
        else:
            items = [{"json": input_data}]
        
        output_items = []
        for idx, item in enumerate(items):
            item_data = item.get("json", item) if isinstance(item, dict) else {}
            
            if keep_input:
                result = {**item_data, **values}
            else:
                result = values
            
            output_items.append(NodeItem(
                json=result,
                pairedItem={"item": idx}
            ))
        
        return NodeExecutionResult(success=True, items=output_items)


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
        
        # Normalize input to items format
        if isinstance(input_data, list):
            items = input_data
        elif isinstance(input_data, dict) and "json" in input_data:
            items = [input_data]
        else:
            items = [{"json": input_data}]
        
        output_items = []
        for idx, item in enumerate(items):
            item_data = item.get("json", item) if isinstance(item, dict) else {}
            
            # Get field value using dot notation
            field_value = item_data
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
            
            output_items.append(NodeItem(
                json=item_data,
                pairedItem={"item": idx}
            ))
        
        # For IF node, all items go to the same output based on first item's condition
        # (n8n behavior - can be enhanced for per-item routing later)
        first_item = items[0] if items else {"json": {}}
        first_data = first_item.get("json", first_item) if isinstance(first_item, dict) else {}
        
        field_value = first_data
        try:
            for key in field_path.split("."):
                field_value = field_value[key]
        except (KeyError, TypeError):
            field_value = None
        
        # Evaluate for routing decision
        route_result = False
        if operator == "equals":
            route_result = str(field_value) == str(compare_value)
        elif operator == "not_equals":
            route_result = str(field_value) != str(compare_value)
        elif operator == "contains":
            route_result = str(compare_value) in str(field_value)
        elif operator == "greater_than":
            try:
                route_result = float(field_value) > float(compare_value)
            except (ValueError, TypeError):
                route_result = False
        elif operator == "less_than":
            try:
                route_result = float(field_value) < float(compare_value)
            except (ValueError, TypeError):
                route_result = False
        elif operator == "is_empty":
            route_result = field_value is None or field_value == "" or field_value == []
        elif operator == "is_not_empty":
            route_result = field_value is not None and field_value != "" and field_value != []
        
        return NodeExecutionResult(
            success=True,
            items=output_items,
            output_handle="true" if route_result else "false"
        )

