"""
Executor Services - High-Level Workflow Execution API

Provides the main entry point for executing workflows.
Integrates compiler, executor, and logging components.

Usage:
    from executor.services import execute_workflow
    
    result = await execute_workflow(
        workflow_id=1,
        user=request.user,
        input_data={"key": "value"},
        trigger_type="manual"
    )
"""
import asyncio
import logging
from typing import Any
from uuid import UUID

from django.db import transaction
from asgiref.sync import sync_to_async

from compiler.validators import (
    validate_dag,
    validate_credentials,
    validate_node_configs,
    topological_sort,
)
from compiler.schemas import (
    ExecutionContext,
    WorkflowExecutionPlan,
    NodeExecutionPlan,
)
from .runner import WorkflowExecutor
from logs.logger import get_execution_logger

logger = logging.getLogger(__name__)


async def execute_workflow(
    workflow_id: int,
    user,
    input_data: dict[str, Any] | None = None,
    trigger_type: str = "manual",
    version_id: int | None = None
) -> dict[str, Any]:
    """
    Main entry point for workflow execution.
    
    This is the high-level service that:
    1. Fetches the workflow (or specific version) from database
    2. Validates and compiles to execution plan
    3. Creates execution log
    4. Runs via WorkflowExecutor
    5. Returns result
    
    Args:
        workflow_id: ID of the workflow to execute
        user: User instance running the workflow
        input_data: Initial input data (e.g., webhook payload)
        trigger_type: How it was triggered ('manual', 'schedule', 'webhook', 'api')
        version_id: Optional specific version number to execute
        
    Returns:
        Dict with execution result
    """
    input_data = input_data or {}
    execution_logger = get_execution_logger()
    
    logger.info(f"Starting workflow execution: workflow_id={workflow_id}, version={version_id}")
    
    # Fetch workflow from database
    try:
        workflow = await sync_to_async(get_workflow)(workflow_id, user)
        nodes = workflow.nodes or []
        edges = workflow.edges or []
        workflow_settings = workflow.workflow_settings or {}
        
        # If version_id specified, load that version
        if version_id:
            # We need to fetch the version
            # Need to import locally to avoid circular imports potentially
            from orchestrator.models import WorkflowVersion
            version = await sync_to_async(WorkflowVersion.objects.get)(
                workflow=workflow, 
                version_number=version_id
            )
            nodes = version.nodes or []
            edges = version.edges or []
            workflow_settings = version.workflow_settings or {}
            
    except Exception as e:
        logger.error(f"Failed to fetch workflow {workflow_id} (v{version_id}): {e}")
        return {
            "success": False,
            "execution_id": None,
            "status": "failed",
            "error": f"Workflow not found: {workflow_id} v{version_id} ({e})",
            "output": {}
        }
    
    # Validate workflow
    validation_errors = []
    validation_errors.extend(validate_dag(nodes, edges))
    validation_errors.extend(validate_node_configs(nodes))
    
    # Get user's credentials for validation
    user_credentials = await sync_to_async(get_user_credentials)(user)
    validation_errors.extend(validate_credentials(nodes, user_credentials))
    
    if validation_errors:
        error_messages = [e.message for e in validation_errors]
        logger.warning(f"Workflow validation failed: {error_messages}")
        return {
            "success": False,
            "execution_id": None,
            "status": "failed",
            "error": f"Validation failed: {'; '.join(error_messages)}",
            "output": {}
        }
    
    # Build execution plan
    execution_order = topological_sort(nodes, edges)
    execution_plan = build_execution_plan(workflow_id, nodes, edges, execution_order)
    
    # Start execution log
    # Include version metadata in input_data for audit (avoiding schema migration for now)
    meta_input = input_data.copy()
    if version_id:
        meta_input['__execution_metadata__'] = {'version_id': version_id}
        
    exec_log = await sync_to_async(execution_logger.start_execution)(
        workflow=workflow,
        user=user,
        trigger_type=trigger_type,
        input_data=meta_input
    )
    
    # Load credentials for context
    credentials = await sync_to_async(load_credentials_for_workflow)(nodes, user)
    
    # Create execution context
    context = ExecutionContext(
        execution_id=exec_log.execution_id,
        user_id=user.id,
        workflow_id=workflow_id,
        workflow_version_id=version_id,
        credentials=credentials
    )
    
    # Create and run executor
    executor = WorkflowExecutor(
        execution_plan=execution_plan,
        edges=edges,
        execution_logger=execution_logger
    )
    
    try:
        output, status = await executor.execute(input_data, context)
        success = status == "completed"
        error_message = output.get("error", "") if not success else ""
        error_node = output.get("failed_node", "") if not success else ""
        
    except Exception as e:
        logger.exception(f"Workflow execution failed unexpectedly: {e}")
        output = {"error": str(e)}
        status = "failed"
        success = False
        error_message = str(e)
        error_node = ""
    
    # Complete execution log
    await sync_to_async(execution_logger.complete_execution)(
        execution_id=exec_log.execution_id,
        output_data=output,
        status=status,
        error_message=error_message,
        error_node_id=error_node
    )
    
    # Update workflow execution count
    await sync_to_async(increment_execution_count)(workflow)
    
    # Reload to get duration
    exec_log = await sync_to_async(ExecutionLog_get)(exec_log.execution_id)
    
    logger.info(
        f"Workflow execution complete: workflow_id={workflow_id}, "
        f"status={status}, duration={exec_log.duration_ms}ms"
    )
    
    return {
        "success": success,
        "execution_id": str(exec_log.execution_id),
        "status": status,
        "output": output,
        "error": error_message or None,
        "duration_ms": exec_log.duration_ms
    }


def get_workflow(workflow_id: int, user):
    """Fetch workflow from database (must be owned by user or public)."""
    from orchestrator.models import Workflow
    
    return Workflow.objects.get(
        id=workflow_id,
        user=user
    )


def get_user_credentials(user) -> set[str]:
    """Get set of credential IDs owned by user."""
    from credentials.models import Credential
    
    creds = Credential.objects.filter(user=user, is_active=True)
    return {str(c.id) for c in creds}


def load_credentials_for_workflow(nodes: list[dict], user) -> dict[str, Any]:
    """
    Load and decrypt credentials needed by workflow nodes.
    
    Returns dict mapping credential_id -> decrypted data
    """
    from credentials.models import Credential
    
    # Find all credential references in node configs
    credential_ids = set()
    for node in nodes:
        config = node.get("data", {}).get("config", {})
        cred_id = config.get("credential")
        if cred_id:
            credential_ids.add(cred_id)
    
    if not credential_ids:
        return {}
    
    # Load credentials from database
    credentials = {}
    creds = Credential.objects.filter(
        user=user,
        id__in=credential_ids,
        is_active=True
    )
    
    for cred in creds:
        # TODO: Implement decryption when Credential model has it
        # For now, return ID as placeholder
        credentials[str(cred.id)] = {
            "id": str(cred.id),
            "name": cred.name,
            # "data": cred.decrypt()  # Future implementation
        }
    
    return credentials


def build_execution_plan(
    workflow_id: int,
    nodes: list[dict],
    edges: list[dict],
    execution_order: list[str]
) -> WorkflowExecutionPlan:
    """
    Build WorkflowExecutionPlan from workflow definition.
    """
    # Build node lookup
    node_lookup = {node['id']: node for node in nodes}
    
    # Build dependency map (which nodes must complete before each node)
    dependencies: dict[str, list[str]] = {nid: [] for nid in execution_order}
    for edge in edges:
        source = edge.get("source")
        target = edge.get("target")
        if target in dependencies:
            dependencies[target].append(source)
    
    # Find entry points (nodes with no dependencies)
    entry_points = [nid for nid, deps in dependencies.items() if not deps]
    
    # Build node execution plans
    node_plans: dict[str, NodeExecutionPlan] = {}
    for node_id in execution_order:
        node = node_lookup.get(node_id, {})
        node_data = node.get("data", {})
        
        node_plans[node_id] = NodeExecutionPlan(
            node_id=node_id,
            node_type=node.get("type", "unknown"),
            config=node_data.get("config", {}),
            dependencies=dependencies.get(node_id, []),
            timeout_seconds=node_data.get("timeout", 60)
        )
    
    return WorkflowExecutionPlan(
        workflow_id=workflow_id,
        execution_order=execution_order,
        nodes=node_plans,
        entry_points=entry_points
    )


def increment_execution_count(workflow):
    """Increment workflow's execution counter."""
    workflow.execution_count = (workflow.execution_count or 0) + 1
    workflow.save(update_fields=['execution_count'])


def ExecutionLog_get(execution_id: UUID):
    """Helper to fetch execution log by ID."""
    from logs.models import ExecutionLog
    return ExecutionLog.objects.get(execution_id=execution_id)
