"""
Logic Node Handlers

Nodes for control flow, loops, and branching.
"""
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

class LoopNode(BaseNodeHandler):
    """
    Execute a loop for a specified number of times or over items.
    
    Supports two modes:
    - Count-based: Loop `max_loop_count` times
    - Item-based: If input contains an array field, iterate over items
    """
    
    node_type = "loop"
    name = "Loop"
    category = NodeCategory.CONDITIONAL.value
    description = "Loop over items or count"
    icon = "🔁"
    color = "#8b5cf6"
    
    fields = [
        FieldConfig(
            name="max_loop_count",
            label="Max Iterations",
            field_type=FieldType.NUMBER,
            default=10,
            required=True,
            description="Maximum number of loop iterations (safety limit)"
        ),
        FieldConfig(
            name="items_field",
            label="Items Field (optional)",
            field_type=FieldType.STRING,
            required=False,
            default="",
            description="Field name containing array to iterate over (leave empty for count-based loop)"
        ),
    ]
    
    outputs = [
        HandleDef(id="loop", label="Loop Body", handle_type="default"),
        HandleDef(id="done", label="Done", handle_type="success"),
    ]
    
    async def execute(
        self,
        input_data: dict[str, Any],
        config: dict[str, Any],
        context: 'ExecutionContext'
    ) -> NodeExecutionResult:
        node_id = context.current_node_id
        max_loop = config.get("max_loop_count", 10)
        items_field = config.get("items_field", "")
        
        # Get current iteration count (0-indexed, represents completed iterations)
        current_count = context.get_loop_count(node_id)
        
        # First iteration: initialize loop state
        if current_count == 0:
            # Check if we have items to iterate over
            items = []
            if items_field and items_field in input_data:
                items = input_data.get(items_field, [])
                if not isinstance(items, list):
                    items = [items]
            else:
                # Auto-detect: find first array in input
                for key, value in input_data.items():
                    if isinstance(value, list) and not key.startswith("_"):
                        items = value
                        break
            
            # Store items for iteration
            context.set_loop_items(node_id, items)
            context.set_batch_cursor(node_id, 0)
        
        # Get stored items
        items = context.get_loop_items(node_id)
        cursor = context.get_batch_cursor(node_id)
        
        # Determine if we should continue looping
        has_items = len(items) > 0
        
        if has_items:
            # Item-based loop
            if cursor < len(items) and current_count < max_loop:
                current_item = items[cursor]
                context.set_batch_cursor(node_id, cursor + 1)
                
                return NodeExecutionResult(
                    success=True,
                    items=[NodeItem(json={
                        "item": current_item,
                        "index": cursor,
                        "total": len(items),
                        **input_data
                    })],
                    output_handle="loop"
                )
            else:
                # Done - return accumulated results
                accumulated = context.get_accumulated_results(node_id)
                return NodeExecutionResult(
                    success=True,
                    items=[NodeItem(json={
                        "results": accumulated if accumulated else [],
                        "iterations": current_count,
                        **input_data
                    })],
                    output_handle="done"
                )
        else:
            # Count-based loop
            if current_count < max_loop:
                return NodeExecutionResult(
                    success=True,
                    items=[NodeItem(json={
                        "index": current_count,
                        "iteration": current_count + 1,
                        **input_data
                    })],
                    output_handle="loop"
                )
            else:
                accumulated = context.get_accumulated_results(node_id)
                return NodeExecutionResult(
                    success=True,
                    items=[NodeItem(json={
                        "results": accumulated if accumulated else [],
                        "iterations": current_count,
                        **input_data
                    })],
                    output_handle="done"
                )


class SplitInBatchesNode(BaseNodeHandler):
    """
    Split input array into batches and process each batch through the loop body.
    
    Automatically detects arrays in input and processes them in chunks.
    """
    
    node_type = "split_in_batches"
    name = "Split In Batches"
    category = NodeCategory.TRANSFORM.value
    description = "Process data in batches"
    icon = "📦"
    color = "#10b981"
    
    fields = [
        FieldConfig(
            name="batch_size",
            label="Batch Size",
            field_type=FieldType.NUMBER,
            default=1,
            required=True,
            description="Number of items per batch"
        ),
        FieldConfig(
            name="max_loop_count",
            label="Max Iterations",
            field_type=FieldType.NUMBER,
            default=100,
            required=True,
            description="Maximum number of batches to process (safety limit)"
        ),
        FieldConfig(
            name="items_field",
            label="Items Field (optional)",
            field_type=FieldType.STRING,
            required=False,
            default="",
            description="Field name containing array to split (auto-detected if empty)"
        ),
    ]
    
    outputs = [
        HandleDef(id="loop", label="Batch", handle_type="default"),
        HandleDef(id="done", label="Done", handle_type="success"),
    ]
    
    async def execute(
        self,
        input_data: dict[str, Any],
        config: dict[str, Any],
        context: 'ExecutionContext'
    ) -> NodeExecutionResult:
        node_id = context.current_node_id
        batch_size = config.get("batch_size", 1)
        max_loop = config.get("max_loop_count", 100)
        items_field = config.get("items_field", "")
        
        # Get current iteration count
        current_count = context.get_loop_count(node_id)
        
        # First iteration: initialize batch state
        if current_count == 0:
            # Find items to batch
            items = []
            if items_field and items_field in input_data:
                items = input_data.get(items_field, [])
                if not isinstance(items, list):
                    items = [items]
            else:
                # Auto-detect: find first array in input
                for key, value in input_data.items():
                    if isinstance(value, list) and not key.startswith("_"):
                        items = value
                        break
            
            # Store items for batching
            context.set_loop_items(node_id, items)
            context.set_batch_cursor(node_id, 0)
        
        # Get stored items and cursor
        items = context.get_loop_items(node_id)
        cursor = context.get_batch_cursor(node_id)
        
        # Calculate number of batches
        total_items = len(items)
        total_batches = (total_items + batch_size - 1) // batch_size if total_items > 0 else 0
        
        # Check if we have more batches to process
        if cursor < total_items and current_count < max_loop:
            # Get current batch
            batch_end = min(cursor + batch_size, total_items)
            current_batch = items[cursor:batch_end]
            
            # Update cursor for next iteration
            context.set_batch_cursor(node_id, batch_end)
            
            return NodeExecutionResult(
                success=True,
                items=[NodeItem(json={
                    "batch": current_batch,
                    "batch_index": current_count,
                    "batch_size": len(current_batch),
                    "total_items": total_items,
                    "total_batches": total_batches,
                    "is_last_batch": batch_end >= total_items,
                    **input_data
                })],
                output_handle="loop"
            )
        else:
            # All batches processed
            accumulated = context.get_accumulated_results(node_id)
            return NodeExecutionResult(
                success=True,
                items=[NodeItem(json={
                    "results": accumulated if accumulated else [],
                    "batches_processed": current_count,
                    "total_items": total_items,
                    **input_data
                })],
                output_handle="done"
            )


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
    
    @staticmethod
    def _eval_condition(field_value: Any, operator: str, compare_value: str) -> bool:
        if operator == "equals":
            return str(field_value) == str(compare_value)
        elif operator == "not_equals":
            return str(field_value) != str(compare_value)
        elif operator == "contains":
            return str(compare_value) in str(field_value)
        elif operator == "greater_than":
            try:
                return float(field_value) > float(compare_value)
            except (ValueError, TypeError):
                return False
        elif operator == "less_than":
            try:
                return float(field_value) < float(compare_value)
            except (ValueError, TypeError):
                return False
        elif operator == "is_empty":
            return field_value is None or field_value == "" or field_value == []
        elif operator == "is_not_empty":
            return field_value is not None and field_value != "" and field_value != []
        return False

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

        true_items: list[NodeItem] = []
        false_items: list[NodeItem] = []

        for idx, item in enumerate(items):
            item_data = item.get("json", item) if isinstance(item, dict) else {}

            # Resolve field value via dot-notation path
            field_value: Any = item_data
            try:
                for key in field_path.split("."):
                    if key:
                        field_value = field_value[key]
            except (KeyError, TypeError):
                field_value = None

            node_item = NodeItem(json=item_data, pairedItem={"item": idx})
            if self._eval_condition(field_value, operator, compare_value):
                true_items.append(node_item)
            else:
                false_items.append(node_item)

        # Route true-matching items to "true"; fall back to "false" only when no true items exist.
        # Note: items routed to the non-chosen branch are not forwarded in this execution cycle
        # because NodeExecutionResult supports a single output_handle per call.
        if true_items:
            return NodeExecutionResult(success=True, items=true_items, output_handle="true")
        return NodeExecutionResult(success=True, items=false_items, output_handle="false")


class StopNode(BaseNodeHandler):
    """
    Explicitly end a workflow path.
    
    Useful for visual clarity and signaling specific termination points.
    """
    
    node_type = "stop"
    name = "Stop"
    category = NodeCategory.CONDITIONAL.value
    description = "Explicitly end the workflow path"
    icon = "🛑"
    color = "#ef4444"  # Red
    
    fields = [
        FieldConfig(
            name="message",
            label="Final Message",
            field_type=FieldType.STRING,
            required=False,
            description="Optional message to log on termination"
        ),
    ]
    
    # End nodes typically don't have outputs, but HandleDef requires them for schema
    outputs = []
    
    async def execute(
        self,
        input_data: dict[str, Any],
        config: dict[str, Any],
        context: 'ExecutionContext'
    ) -> NodeExecutionResult:
        message = config.get("message", "Workflow reached explicit stop.")
        
        return NodeExecutionResult(
            success=True,
            items=[NodeItem(json={"status": "stopped", "message": message})],
            output_handle=None  # Signaling no further routing
        )
