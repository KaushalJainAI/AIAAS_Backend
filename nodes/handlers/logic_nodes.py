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
                    data={
                        "item": current_item,
                        "index": cursor,
                        "total": len(items),
                        **input_data
                    },
                    output_handle="loop"
                )
            else:
                # Done - return accumulated results
                accumulated = context.get_accumulated_results(node_id)
                return NodeExecutionResult(
                    success=True,
                    data={
                        "results": accumulated if accumulated else [],
                        "iterations": current_count,
                        **input_data
                    },
                    output_handle="done"
                )
        else:
            # Count-based loop
            if current_count < max_loop:
                return NodeExecutionResult(
                    success=True,
                    data={
                        "index": current_count,
                        "iteration": current_count + 1,
                        **input_data
                    },
                    output_handle="loop"
                )
            else:
                accumulated = context.get_accumulated_results(node_id)
                return NodeExecutionResult(
                    success=True,
                    data={
                        "results": accumulated if accumulated else [],
                        "iterations": current_count,
                        **input_data
                    },
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
                data={
                    "batch": current_batch,
                    "batch_index": current_count,
                    "batch_size": len(current_batch),
                    "total_items": total_items,
                    "total_batches": total_batches,
                    "is_last_batch": batch_end >= total_items,
                    **input_data
                },
                output_handle="loop"
            )
        else:
            # All batches processed
            accumulated = context.get_accumulated_results(node_id)
            return NodeExecutionResult(
                success=True,
                data={
                    "results": accumulated if accumulated else [],
                    "batches_processed": current_count,
                    "total_items": total_items,
                    **input_data
                },
                output_handle="done"
            )

