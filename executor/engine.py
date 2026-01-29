"""
Execution Engine (The Worker)

This module is responsible for the deterministic execution of workflows.
It accepts a compiled workflow (or JSON) and executes it reliably, treating
the workflow as a "Glass Box" with full observability.

It does NOT make high-level decisions or handle user intent - that is the job
of the Orchestrator (King Agent).
"""
import asyncio
import logging
from uuid import UUID
from datetime import datetime
from typing import Any, Dict

from django.utils import timezone

from compiler.compiler import WorkflowCompiler, WorkflowCompilationError
from logs.models import ExecutionLog
from logs.logger import ExecutionLogger
from orchestrator.interface import OrchestratorInterface, ExecutionState

logger = logging.getLogger(__name__)

class ExecutionEngine:
    """
    Deterministic Workflow Execution Engine.
    
    Responsibilities:
    1. Compile Workflow JSON -> LangGraph
    2. Execute the Graph
    3. separate Execution State management
    """
    
    def __init__(self, orchestrator: OrchestratorInterface | None = None):
        """
        Args:
            orchestrator: Optional supervisor to handle hooks (before_node, on_error, etc.)
        """
        self.orchestrator = orchestrator

    async def run_workflow(
        self,
        execution_id: UUID,
        workflow_id: int,
        user_id: int,
        workflow_json: dict,
        input_data: dict[str, Any],
        credentials: dict[str, Any],
        parent_execution_id: UUID | None = None,
        nesting_depth: int = 0,
        workflow_chain: list[int] | None = None,
        timeout_budget_ms: int | None = None
    ) -> ExecutionState:
        """
        Run a workflow from start to finish (or until paused/failed).
        """
        logger.info(f"Engine starting execution {execution_id} for workflow {workflow_id}")
        
        # 1. Compile & Build Graph (Single Pass)
        try:
            # Pass used credentials so they are validated
            used_creds = set(credentials.keys()) if credentials else set()
            compiler = WorkflowCompiler(workflow_json, user=None, user_credentials=used_creds)
            
            # Direct compilation to StateGraph
            # We pass self.orchestrator to allow the compiled nodes to call back hooks
            graph = compiler.compile(orchestrator=self.orchestrator)
            
        except WorkflowCompilationError as e:
            logger.error(f"Compilation failed for execution {execution_id}: {e}")
            return ExecutionState.FAILED
        except Exception as e:
            logger.exception(f"Unexpected compilation error for execution {execution_id}")
            return ExecutionState.FAILED

        # 2. Create Logger
        exec_logger = ExecutionLogger()
        await exec_logger.start_execution(
            execution_id=execution_id,
            workflow_id=workflow_id,
            user_id=user_id,
            trigger_type="orchestrator",
            parent_execution_id=parent_execution_id,
            nesting_depth=nesting_depth,
            timeout_budget_ms=timeout_budget_ms,
            workflow_snapshot=workflow_json
        )
        
        # 3. Prepare Initial State
        initial_state = {
            "execution_id": str(execution_id),
            "user_id": user_id,
            "workflow_id": workflow_id,
            "current_node": "",
            "node_outputs": {}, 
            "variables": {},
            "credentials": credentials,
            "error": None,
            "status": "running",
            "nesting_depth": nesting_depth,
            "workflow_chain": workflow_chain or [],
            "parent_execution_id": str(parent_execution_id) if parent_execution_id else None,
            "timeout_budget_ms": timeout_budget_ms
        }
        
        # Map input to entry points (if needed, or just dump in node_outputs)
        # For simplicity, we put input in special keys as before
        # Ideally the specific trigger nodes pull this out
        initial_state["node_outputs"]["_input_global"] = input_data
        
        try:
            # 4. Invoke Graph
            # This is where the magic happens. LangGraph runs the DAG.
            # It will block until completion or a handled interruption (managed by orchestrator hooks / wait)
            final_state = await graph.ainvoke(initial_state)
            
            # 5. Handle Result
            final_status = final_state.get("status", "completed")
            error = final_state.get("error")
            
            # Log completion
            await exec_logger.complete_execution(
                execution_id=execution_id,
                status=final_status,
                output=final_state.get("node_outputs", {}),
                error_message=error if error else ""
            )
            
            if final_status == "failed":
                return ExecutionState.FAILED
            elif final_status == "cancelled":
                return ExecutionState.CANCELLED
            elif final_status == "paused":
                 return ExecutionState.PAUSED
            else:
                return ExecutionState.COMPLETED

        except Exception as e:
            logger.exception(f"Engine crashed during execution {execution_id}")
            await exec_logger.complete_execution(
                execution_id=execution_id,
                status="failed",
                error_message=str(e)
            )
            return ExecutionState.FAILED
