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
            import uuid
            from asgiref.sync import sync_to_async
            from executor.engine import ExecutionEngine
            from orchestrator.interface import SupervisionLevel
            from .base import NodeItem

            # Fetch target workflow definition from DB
            @sync_to_async
            def fetch_workflow(wf_id):
                from orchestrator.models import Workflow
                return Workflow.objects.get(id=wf_id)

            try:
                target_workflow = await fetch_workflow(workflow_id)
            except Exception:
                return NodeExecutionResult(
                    success=False,
                    error=f"Workflow {workflow_id} not found",
                    output_handle="error"
                )

            sub_execution_id = uuid.uuid4()
            child_chain = list(getattr(context, 'workflow_chain', [])) + [context.workflow_id]

            engine = ExecutionEngine(orchestrator=None)
            state = await engine.run_workflow(
                execution_id=sub_execution_id,
                workflow_id=int(workflow_id),
                user_id=context.user_id,
                workflow_json=target_workflow.workflow_json,
                input_data=sub_input,
                credentials=context.credentials,
                parent_execution_id=context.execution_id,
                nesting_depth=nesting_depth + 1,
                workflow_chain=child_chain,
                supervision_level=SupervisionLevel.NONE,
                skills=list(context.skills) if context.skills else [],
            )

            from orchestrator.interface import ExecutionState
            if state != ExecutionState.COMPLETED:
                return NodeExecutionResult(
                    success=False,
                    error=f"Subworkflow {workflow_id} finished with state: {state}",
                    output_handle="error"
                )

            # Retrieve last node outputs from the sub-execution log
            @sync_to_async
            def fetch_last_output(exec_id):
                from logs.models import ExecutionLog
                try:
                    log = ExecutionLog.objects.get(execution_id=exec_id)
                    return log.result_data or {}
                except ExecutionLog.DoesNotExist:
                    return {}

            sub_output = await fetch_last_output(sub_execution_id)

            # 5. Output Mapping
            output_mapping = config.get("output_mapping", {})
            final_output = self._transform_state(sub_output, output_mapping)

            return NodeExecutionResult(
                success=True,
                items=[NodeItem(json=final_output)],
                output_handle="success"
            )

        except Exception as e:
            return NodeExecutionResult(
                success=False,
                error=str(e),
                output_handle="error"
            )
