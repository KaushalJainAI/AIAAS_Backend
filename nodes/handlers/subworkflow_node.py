"""
Subworkflow Node Handler
"""
from typing import Any, TYPE_CHECKING
from .base import (
    BaseNodeHandler,
    NodeCategory,
    FieldConfig,
    FieldType,
    HandleDef,
    NodeExecutionResult
)

if TYPE_CHECKING:
    from compiler.schemas import ExecutionContext

class SubworkflowNodeHandler(BaseNodeHandler):
    """
    Execute another workflow as a node.
    """
    
    node_type = "subworkflow"
    name = "Execute Workflow"
    category = NodeCategory.ACTION.value
    description = "Execute another workflow within the current one"
    icon = "⚡"
    color = "#8b5cf6"
    
    fields = [
        FieldConfig(
            name="workflow_id",
            label="Workflow ID",
            field_type=FieldType.STRING,  # Should ideally be a selector
            required=True,
            description="ID of the workflow to execute"
        ),
        FieldConfig(
            name="input_mapping",
            label="Input Mapping",
            field_type=FieldType.JSON,
            default={},
            description="Map parent fields to child input"
        ),
        FieldConfig(
            name="output_mapping",
            label="Output Mapping",
            field_type=FieldType.JSON,
            default={},
            description="Map child output to parent fields"
        )
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
        workflow_id = config.get("workflow_id")
        
        # 1. Circular Dependency Check
        if self._detect_circular_dependency(workflow_id, context):
            return NodeExecutionResult(
                success=False,
                error=f"Circular dependency detected: Workflow {workflow_id} is already in the chain.",
                output_handle="error"
            )
            
        # 2. Nesting Depth Check
        nesting_depth = getattr(context, 'nesting_depth', 0)
        max_depth = getattr(context, 'max_nesting_depth', 3)
        
        if nesting_depth >= max_depth:
            return NodeExecutionResult(
                success=False,
                error=f"Max nesting depth ({max_depth}) exceeded.",
                output_handle="error"
            )

        # 3. Input Mapping
        mapping = config.get("input_mapping", {})
        sub_input = self._transform_state(input_data, mapping)
        
        # 4. Execute Subworkflow
        try:
            # Dynamic import to break cycle
            from executor.orchestrator import WorkflowOrchestrator
            orch = WorkflowOrchestrator.get_instance()
            
            # Execute subworkflow using orchestrator
            # Note: We need to ensure execute_subworkflow exists on Orchestrator
            result = await orch.execute_subworkflow(context, config, sub_input)
            
            if not result.success:
                 return NodeExecutionResult(
                    success=False,
                    error=result.error or "Subworkflow failed",
                    output_handle="error"
                )
                
            # 5. Output Mapping
            output_mapping = config.get("output_mapping", {})
            final_output = self._transform_state(result.data, output_mapping)
            
            return NodeExecutionResult(
                success=True,
                data=final_output,
                output_handle="success"
            )
            
        except ImportError:
            return NodeExecutionResult(
                success=False, 
                error="Subworkflow executor not implemented (ImportError)",
                output_handle="error"
            )
        except Exception as e:
            return NodeExecutionResult(
                success=False,
                error=str(e),
                output_handle="error"
            )
