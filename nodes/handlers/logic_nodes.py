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
            description="Protect against infinite loops"
        ),
        # Add other config like 'batch_size' or 'mode' if needed
    ]
    
    outputs = [
        HandleDef(id="loop", label="Loop", handle_type="default"),
        HandleDef(id="done", label="Done", handle_type="success"),
    ]
    
    async def execute(
        self,
        input_data: dict[str, Any],
        config: dict[str, Any],
        context: 'ExecutionContext'
    ) -> NodeExecutionResult:
        max_loop = config.get("max_loop_count", 10)
        current_count = context.loop_stats.get(context.current_node_id, 0)
        
        # Increment happening in Orchestrator usually, but if we do it here:
        # We rely on Orchestrator tracking or we track in context.
        # Let's assume Orchestrator did it or we do it now. 
        # Actually checking validation logic: if we are here, we are running.
        
        # If we exceeded logic:
        # Note: Implementation logic allows looping until max.
        # If current_count < max_loop: return 'loop' handle.
        # Else: return 'done'.
        
        if current_count < max_loop:
             # In a real system, we'd slice input data here.
             return NodeExecutionResult(
                 success=True,
                 data=input_data, # Pass through data to loop body
                 output_handle="loop"
             )
        else:
             return NodeExecutionResult(
                 success=True,
                 data=input_data,
                 output_handle="done"
             )

class SplitInBatchesNode(BaseNodeHandler):
    """
    Split input array into batches and loop.
    """
    
    node_type = "split_in_batches"
    name = "Split In Batches"
    category = NodeCategory.TRANSFORM.value # Or conditional
    description = "Process data in batches"
    icon = "📦"
    color = "#10b981"
    
    fields = [
        FieldConfig(
            name="batch_size",
            label="Batch Size",
            field_type=FieldType.NUMBER,
            default=1,
            required=True
        ),
        FieldConfig(
            name="max_loop_count",
            label="Max Iterations",
            field_type=FieldType.NUMBER,
            default=100,
            required=True,
            description="Safety limit"
        )
    ]
    
    outputs = [
        HandleDef(id="loop", label="Loop", handle_type="default"),
        HandleDef(id="done", label="Done", handle_type="success"),
    ]
    
    async def execute(
        self,
        input_data: dict[str, Any],
        config: dict[str, Any],
        context: 'ExecutionContext'
    ) -> NodeExecutionResult:
         # Simplified logic: 
         # Real implementation would manage state (cursor) in context.variables or similar.
         # For now, we respect the loop count limit as requested.
        
        max_loop = config.get("max_loop_count", 100)
        current_count = context.loop_stats.get(context.current_node_id, 0)
        
        if current_count < max_loop:
             return NodeExecutionResult(
                 success=True,
                 data=input_data,
                 output_handle="loop"
             )
        else:
             return NodeExecutionResult(
                 success=True,
                 data=input_data,
                 output_handle="done"
             )
