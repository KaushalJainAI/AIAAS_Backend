"""
Deterministic workflow execution engine.

This is the "worker" half of the engine/king split. It consumes a compiled
LangGraph StateGraph and runs it against a prepared state, emitting logs
and heartbeats along the way.

High-level decisions (user intent, HITL, workflow generation) live in
KingOrchestrator. The engine never makes those calls — it only invokes the
graph and reports the outcome.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any
from uuid import UUID

from asgiref.sync import sync_to_async

from compiler.compiler import WorkflowCompiler, WorkflowCompilationError
from logs.logger import ExecutionLogger
from orchestrator.interface import (
    ExecutionState, OrchestratorInterface, SupervisionLevel,
)

logger = logging.getLogger(__name__)

# How often to update the execution's heartbeat while the graph runs.
# Short enough that the zombie reaper (5-min cutoff) won't false-positive;
# long enough to avoid pointless DB writes.
_HEARTBEAT_INTERVAL_S = 30


def _initial_state(
    execution_id: UUID,
    workflow_id: int,
    user_id: int,
    *,
    input_data: dict | None,
    credentials: dict | None,
    parent_execution_id: UUID | None,
    nesting_depth: int,
    workflow_chain: list[int] | None,
    timeout_budget_ms: int | None,
    skills: list[dict] | None,
) -> dict:
    """Build the initial WorkflowState dict the compiled graph expects."""
    return {
        "execution_id": str(execution_id),
        "user_id": user_id,
        "workflow_id": workflow_id,
        "current_node": "",
        "node_outputs": {"_input_global": input_data or {}},
        "variables": {},
        "credentials": credentials or {},
        "loop_stats": {},
        "error": None,
        "status": "running",
        "nesting_depth": nesting_depth,
        "workflow_chain": workflow_chain or [],
        "parent_execution_id": str(parent_execution_id) if parent_execution_id else None,
        "timeout_budget_ms": timeout_budget_ms,
        "skills": skills or [],
    }


def _result_to_execution_state(status: str) -> ExecutionState:
    """Map a workflow-state status string to an ExecutionState enum value."""
    mapping = {
        "failed": ExecutionState.FAILED,
        "cancelled": ExecutionState.CANCELLED,
        "paused": ExecutionState.PAUSED,
    }
    return mapping.get(status, ExecutionState.COMPLETED)


class ExecutionEngine:
    """
    Deterministic Workflow Execution Engine.

    1. Compile workflow JSON → LangGraph
    2. Execute the graph
    3. Manage execution lifecycle logging
    """

    def __init__(self, orchestrator: OrchestratorInterface | None = None):
        self.orchestrator = orchestrator

    async def run_workflow(
        self,
        execution_id: UUID,
        workflow_id: int,
        user_id: int,
        workflow_json: dict,
        input_data: dict[str, Any] | None = None,
        credentials: dict[str, Any] | None = None,
        parent_execution_id: UUID | None = None,
        nesting_depth: int = 0,
        workflow_chain: list[int] | None = None,
        timeout_budget_ms: int | None = None,
        supervision_level: "SupervisionLevel" = None,
        skills: list[dict] | None = None,
    ) -> ExecutionState:
        """
        Run a workflow end-to-end (or until paused / cancelled / failed).
        """
        logger.info(
            f"Engine starting execution {execution_id} for workflow "
            f"{workflow_id} (supervision={supervision_level})"
        )
        exec_logger = ExecutionLogger()

        # NONE supervision means no orchestrator hooks at all.
        effective_orchestrator = (
            None if supervision_level == SupervisionLevel.NONE else self.orchestrator
        )

        graph = await self._compile(
            workflow_json, user_id, effective_orchestrator, supervision_level,
            execution_id, exec_logger,
        )
        if graph is None:
            return ExecutionState.FAILED

        initial_state = _initial_state(
            execution_id, workflow_id, user_id,
            input_data=input_data, credentials=credentials,
            parent_execution_id=parent_execution_id,
            nesting_depth=nesting_depth,
            workflow_chain=workflow_chain,
            timeout_budget_ms=timeout_budget_ms,
            skills=skills,
        )

        async with _heartbeat(exec_logger, execution_id):
            return await self._invoke_graph(graph, initial_state, exec_logger, execution_id)

    async def _compile(
        self, workflow_json, user_id, orchestrator, supervision_level,
        execution_id, exec_logger,
    ):
        """Compile inside sync_to_async; log and return None on failure."""
        from credentials.models import Credential

        @sync_to_async
        def _do():
            cred_ids = set(map(
                str,
                Credential.objects.filter(user_id=user_id).values_list("id", flat=True),
            ))
            compiler = WorkflowCompiler(workflow_json, user=None, user_credentials=cred_ids)
            return compiler.compile(
                orchestrator=orchestrator, supervision_level=supervision_level,
            )

        try:
            return await _do()
        except WorkflowCompilationError as e:
            await exec_logger.complete_execution(
                execution_id=execution_id, status="failed",
                error_message=f"Compilation failed: {e}",
            )
            logger.error(f"Compilation failed for execution {execution_id}: {e}")
            return None
        except Exception as e:
            await exec_logger.complete_execution(
                execution_id=execution_id, status="failed",
                error_message=f"Unexpected compilation error: {e}",
            )
            logger.exception(f"Unexpected compilation error for execution {execution_id}")
            return None

    async def _invoke_graph(
        self, graph, initial_state, exec_logger, execution_id,
    ) -> ExecutionState:
        try:
            final_state = await graph.ainvoke(initial_state)
        except asyncio.CancelledError:
            logger.info(f"Engine execution {execution_id} cancelled")
            await exec_logger.complete_execution(
                execution_id=execution_id, status="cancelled",
                error_message="Execution cancelled by user",
            )
            raise
        except Exception as e:
            logger.exception(f"Engine crashed during execution {execution_id}")
            await exec_logger.complete_execution(
                execution_id=execution_id, status="failed", error_message=str(e),
            )
            return ExecutionState.FAILED

        # Normalise "still running" to "completed" — the graph finished.
        status = final_state.get("status") or "completed"
        if status == "running":
            status = "completed"

        await exec_logger.complete_execution(
            execution_id=execution_id, status=status,
            output_data=final_state.get("node_outputs", {}),
            error_message=final_state.get("error") or "",
        )
        return _result_to_execution_state(status)


class _heartbeat:
    """
    Async context manager that pings the execution logger at a fixed interval
    while the wrapped block runs. Cancels cleanly on exit.
    """
    def __init__(self, exec_logger: ExecutionLogger, execution_id: UUID):
        self._logger = exec_logger
        self._execution_id = execution_id
        self._task: asyncio.Task | None = None

    async def _pulse(self) -> None:
        try:
            while True:
                await asyncio.sleep(_HEARTBEAT_INTERVAL_S)
                await self._logger.heartbeat(self._execution_id)
        except asyncio.CancelledError:
            pass

    async def __aenter__(self) -> "_heartbeat":
        self._task = asyncio.create_task(self._pulse())
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
