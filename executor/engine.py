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
from orchestrator.interface import OrchestratorInterface, ExecutionState, SupervisionLevel

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
        timeout_budget_ms: int | None = None,
        supervision_level: 'SupervisionLevel' = None,
    ) -> ExecutionState:
        """
        Run a workflow from start to finish (or until paused/failed).
        
        Args:
            supervision_level: Level of orchestrator supervision.
                - FULL: All hooks called
                - ERROR_ONLY: Only on_error hook
                - NONE: No hooks (pure execution)
        """
        logger.info(f"Engine starting execution {execution_id} for workflow {workflow_id} (supervision={supervision_level})")
        
        # 1. Create Logger (Start monitoring IMMEDIATELY)
        exec_logger = ExecutionLogger()
        # NOTE: ExecutionLog is now created by KingOrchestrator to prevent race conditions.
        # We do NOT call start_execution_async here anymore.

        # Determine orchestrator to pass based on supervision level
        effective_orchestrator = self.orchestrator
        if supervision_level == SupervisionLevel.NONE:
            effective_orchestrator = None  # No hooks at all
        
        # 2. Compile & Build Graph (Single Pass)
        graph = None
        try:
            # Pass used credentials so they are validated
            used_creds = set(credentials.keys()) if credentials else set()
            compiler = WorkflowCompiler(workflow_json, user=None, user_credentials=used_creds)
            
            # Direct compilation to StateGraph
            # Pass orchestrator and supervision level for hook filtering
            graph = compiler.compile(
                orchestrator=effective_orchestrator,
                supervision_level=supervision_level
            )
            
        except WorkflowCompilationError as e:
            error_msg = f"Compilation failed: {e}"
            logger.error(f"{error_msg} for execution {execution_id}")
            await exec_logger.complete_execution(
                execution_id=execution_id,
                status="failed",
                error_message=error_msg
            )
            return ExecutionState.FAILED
        except Exception as e:
            error_msg = f"Unexpected compilation error: {str(e)}"
            logger.exception(f"{error_msg} for execution {execution_id}")
            await exec_logger.complete_execution(
                execution_id=execution_id,
                status="failed",
                error_message=error_msg
            )
            return ExecutionState.FAILED
        
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
            if final_status == "running":
                # If it's still 'running' but the graph finished, it's successful
                final_status = "completed"
                
            error = final_state.get("error")
            
            # Log completion
            await exec_logger.complete_execution(
                execution_id=execution_id,
                status=final_status,
                output_data=final_state.get("node_outputs", {}),
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

        except asyncio.CancelledError:
            logger.info(f"Engine execution {execution_id} cancelled")
            await exec_logger.complete_execution(
                execution_id=execution_id,
                status="cancelled",
                error_message="Execution cancelled by user"
            )
            raise
        except Exception as e:
            logger.exception(f"Engine crashed during execution {execution_id}")
            await exec_logger.complete_execution(
                execution_id=execution_id,
                status="failed",
                error_message=str(e)
            )
            return ExecutionState.FAILED
