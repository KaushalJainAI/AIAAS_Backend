"""
Workflow Orchestrator

High-level workflow orchestration with stop/pause/resume control,
human-in-the-loop integration, and AI workflow generation.
"""
import asyncio
import logging
from datetime import datetime
from enum import Enum
from typing import Any, Callable
from uuid import UUID, uuid4
from dataclasses import dataclass, field

from django.utils import timezone

from compiler.schemas import ExecutionContext, WorkflowExecutionPlan
from compiler.compiler import WorkflowCompiler
from executor.runner import WorkflowExecutor
from logs.models import ExecutionLog
from logs.logger import ExecutionLogger

logger = logging.getLogger(__name__)


class ExecutionState(str, Enum):
    """Possible states of a workflow execution."""
    PENDING = "pending"
    RUNNING = "running"
    PAUSED = "paused"
    WAITING_HUMAN = "waiting_human"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class HITLRequestType(str, Enum):
    """Types of human-in-the-loop requests."""
    APPROVAL = "approval"
    CLARIFICATION = "clarification"
    ERROR_RECOVERY = "error_recovery"
    REVIEW = "review"


@dataclass
class HITLRequest:
    """A human-in-the-loop interaction request."""
    id: str
    request_type: HITLRequestType
    node_id: str
    message: str
    options: list[str] = field(default_factory=list)
    timeout_seconds: int = 300
    created_at: datetime = field(default_factory=datetime.utcnow)
    response: Any = None
    responded_at: datetime | None = None


@dataclass
class ExecutionHandle:
    """Handle for controlling a running workflow."""
    execution_id: UUID
    workflow_id: int
    user_id: int
    state: ExecutionState = ExecutionState.PENDING
    current_node: str | None = None
    progress: float = 0.0
    started_at: datetime | None = None
    completed_at: datetime | None = None
    error: str | None = None
    pending_hitl: HITLRequest | None = None


class WorkflowOrchestrator:
    """
    Orchestrates workflow execution with full control capabilities.
    
    Features:
    - Start/Stop/Pause/Resume execution
    - Human-in-the-loop blocking and async approval
    - Progress tracking and streaming
    - Error handling with recovery options
    
    Usage:
        orchestrator = WorkflowOrchestrator()
        handle = await orchestrator.start(workflow, user_id)
        await orchestrator.pause(handle.execution_id)
        await orchestrator.resume(handle.execution_id)
    """
    
    def __init__(self):
        # Track active executions
        self._executions: dict[UUID, ExecutionHandle] = {}
        self._tasks: dict[UUID, asyncio.Task] = {}
        self._pause_events: dict[UUID, asyncio.Event] = {}
        self._hitl_responses: dict[str, asyncio.Queue] = {}
        
        # Callbacks for external integration
        self._on_state_change: Callable[[ExecutionHandle], None] | None = None
        self._on_progress: Callable[[UUID, str, float], None] | None = None
        self._on_hitl_request: Callable[[HITLRequest], None] | None = None
    
    def set_callbacks(
        self,
        on_state_change: Callable[[ExecutionHandle], None] | None = None,
        on_progress: Callable[[UUID, str, float], None] | None = None,
        on_hitl_request: Callable[[HITLRequest], None] | None = None,
    ) -> None:
        """Set callback functions for external integration (SSE, WebSocket)."""
        self._on_state_change = on_state_change
        self._on_progress = on_progress
        self._on_hitl_request = on_hitl_request
    
    async def start(
        self,
        workflow_json: dict,
        user_id: int,
        input_data: dict[str, Any] | None = None,
        credentials: dict[str, Any] | None = None,
    ) -> ExecutionHandle:
        """
        Start a new workflow execution.
        
        Args:
            workflow_json: The workflow definition (nodes, edges)
            user_id: User who initiated the execution
            input_data: Initial input data for triggers
            credentials: Pre-loaded credentials (decrypted)
            
        Returns:
            ExecutionHandle for tracking and control
        """
        execution_id = uuid4()
        workflow_id = workflow_json.get('id', 0)
        
        # Create handle
        handle = ExecutionHandle(
            execution_id=execution_id,
            workflow_id=workflow_id,
            user_id=user_id,
            state=ExecutionState.PENDING,
            started_at=timezone.now(),
        )
        self._executions[execution_id] = handle
        
        # Create pause event (set = running, clear = paused)
        pause_event = asyncio.Event()
        pause_event.set()
        self._pause_events[execution_id] = pause_event
        
        # Start execution task
        task = asyncio.create_task(
            self._run_workflow(
                handle,
                workflow_json,
                input_data or {},
                credentials or {},
            )
        )
        self._tasks[execution_id] = task
        
        logger.info(f"Started workflow execution: {execution_id}")
        return handle
    
    async def _run_workflow(
        self,
        handle: ExecutionHandle,
        workflow_json: dict,
        input_data: dict[str, Any],
        credentials: dict[str, Any],
    ) -> None:
        """Internal workflow execution loop."""
        execution_id = handle.execution_id
        
        try:
            # Update state
            handle.state = ExecutionState.RUNNING
            self._notify_state_change(handle)
            
            # Compile workflow
            compiler = WorkflowCompiler()
            compile_result = compiler.compile(workflow_json)
            
            if not compile_result.success:
                handle.state = ExecutionState.FAILED
                handle.error = "; ".join([e.message for e in compile_result.errors])
                self._notify_state_change(handle)
                return
            
            # Create execution context
            context = ExecutionContext(
                execution_id=execution_id,
                user_id=handle.user_id,
                workflow_id=handle.workflow_id,
                credentials=credentials,
            )
            
            # Create logger
            exec_logger = ExecutionLogger()
            await exec_logger.start_execution(
                execution_id=execution_id,
                workflow_id=handle.workflow_id,
                user_id=handle.user_id,
                trigger_type="orchestrator"
            )
            
            # Create executor with our custom node runner that supports pause
            execution_plan = WorkflowExecutionPlan(**compile_result.execution_plan)
            edges = workflow_json.get('edges', [])
            
            executor = WorkflowExecutor(
                execution_plan=execution_plan,
                edges=edges,
                execution_logger=exec_logger
            )
            
            # Execute with pause support
            total_nodes = len(execution_plan.execution_order)
            
            for i, node_id in enumerate(execution_plan.execution_order):
                # Check for pause
                pause_event = self._pause_events.get(execution_id)
                if pause_event and not pause_event.is_set():
                    handle.state = ExecutionState.PAUSED
                    self._notify_state_change(handle)
                    await pause_event.wait()
                    handle.state = ExecutionState.RUNNING
                    self._notify_state_change(handle)
                
                # Check for cancellation
                if handle.state == ExecutionState.CANCELLED:
                    break
                
                # Update progress
                handle.current_node = node_id
                handle.progress = (i / total_nodes) * 100
                self._notify_progress(execution_id, node_id, handle.progress)
                
                # Get node plan and execute
                node_plan = execution_plan.nodes.get(node_id)
                if node_plan:
                    # Check if node requires HITL
                    if node_plan.config.get('requires_approval'):
                        await self._request_approval(handle, node_id, node_plan)
            
            # Execute the full workflow
            final_output, status = await executor.execute(input_data, context)
            
            # Update final state
            if status == "completed":
                handle.state = ExecutionState.COMPLETED
            elif status == "cancelled":
                handle.state = ExecutionState.CANCELLED
            else:
                handle.state = ExecutionState.FAILED
                handle.error = final_output.get('error')
            
            handle.progress = 100.0
            handle.completed_at = timezone.now()
            
            # Complete logging
            await exec_logger.complete_execution(
                execution_id=execution_id,
                status=handle.state.value,
                output=final_output
            )
            
        except asyncio.CancelledError:
            handle.state = ExecutionState.CANCELLED
            logger.info(f"Execution {execution_id} was cancelled")
        except Exception as e:
            handle.state = ExecutionState.FAILED
            handle.error = str(e)
            logger.exception(f"Execution {execution_id} failed: {e}")
        finally:
            self._notify_state_change(handle)
            handle.completed_at = timezone.now()
    
    async def pause(self, execution_id: UUID) -> bool:
        """
        Pause a running execution.
        
        Args:
            execution_id: The execution to pause
            
        Returns:
            True if paused successfully
        """
        handle = self._executions.get(execution_id)
        if not handle or handle.state != ExecutionState.RUNNING:
            return False
        
        pause_event = self._pause_events.get(execution_id)
        if pause_event:
            pause_event.clear()
            logger.info(f"Pausing execution {execution_id}")
            return True
        return False
    
    async def resume(self, execution_id: UUID) -> bool:
        """
        Resume a paused execution.
        
        Args:
            execution_id: The execution to resume
            
        Returns:
            True if resumed successfully
        """
        handle = self._executions.get(execution_id)
        if not handle or handle.state != ExecutionState.PAUSED:
            return False
        
        pause_event = self._pause_events.get(execution_id)
        if pause_event:
            pause_event.set()
            logger.info(f"Resuming execution {execution_id}")
            return True
        return False
    
    async def stop(self, execution_id: UUID) -> bool:
        """
        Stop/cancel a running execution.
        
        Args:
            execution_id: The execution to stop
            
        Returns:
            True if stopped successfully
        """
        handle = self._executions.get(execution_id)
        if not handle:
            return False
        
        if handle.state in (ExecutionState.COMPLETED, ExecutionState.FAILED, ExecutionState.CANCELLED):
            return False
        
        handle.state = ExecutionState.CANCELLED
        
        # Cancel the task
        task = self._tasks.get(execution_id)
        if task and not task.done():
            task.cancel()
        
        # Resume if paused to allow cancellation
        pause_event = self._pause_events.get(execution_id)
        if pause_event:
            pause_event.set()
        
        logger.info(f"Stopped execution {execution_id}")
        self._notify_state_change(handle)
        return True
    
    async def _request_approval(
        self,
        handle: ExecutionHandle,
        node_id: str,
        node_plan: Any,
    ) -> bool:
        """Request human approval for a node."""
        request = HITLRequest(
            id=str(uuid4()),
            request_type=HITLRequestType.APPROVAL,
            node_id=node_id,
            message=f"Approval required for node: {node_plan.config.get('name', node_id)}",
            options=["approve", "reject"],
            timeout_seconds=node_plan.config.get('approval_timeout', 300),
        )
        
        handle.state = ExecutionState.WAITING_HUMAN
        handle.pending_hitl = request
        self._notify_state_change(handle)
        
        # Create response queue
        response_queue = asyncio.Queue()
        self._hitl_responses[request.id] = response_queue
        
        # Notify external handlers
        if self._on_hitl_request:
            self._on_hitl_request(request)
        
        try:
            # Wait for response with timeout
            response = await asyncio.wait_for(
                response_queue.get(),
                timeout=request.timeout_seconds
            )
            
            request.response = response
            request.responded_at = datetime.utcnow()
            
            handle.pending_hitl = None
            handle.state = ExecutionState.RUNNING
            self._notify_state_change(handle)
            
            return response.get('action') == 'approve'
            
        except asyncio.TimeoutError:
            logger.warning(f"HITL request {request.id} timed out")
            handle.pending_hitl = None
            handle.state = ExecutionState.FAILED
            handle.error = "Approval timeout"
            self._notify_state_change(handle)
            return False
        finally:
            self._hitl_responses.pop(request.id, None)
    
    async def respond_to_hitl(
        self,
        request_id: str,
        response: dict[str, Any],
    ) -> bool:
        """
        Respond to a HITL request.
        
        Args:
            request_id: The HITL request ID
            response: Response data (e.g., {"action": "approve"})
            
        Returns:
            True if response was delivered
        """
        queue = self._hitl_responses.get(request_id)
        if queue:
            await queue.put(response)
            return True
        return False
    
    def get_status(self, execution_id: UUID) -> ExecutionHandle | None:
        """Get the current status of an execution."""
        return self._executions.get(execution_id)
    
    def get_all_active(self, user_id: int | None = None) -> list[ExecutionHandle]:
        """Get all active executions, optionally filtered by user."""
        active_states = {ExecutionState.RUNNING, ExecutionState.PAUSED, ExecutionState.WAITING_HUMAN}
        result = []
        
        for handle in self._executions.values():
            if handle.state in active_states:
                if user_id is None or handle.user_id == user_id:
                    result.append(handle)
        
        return result
    
    def _notify_state_change(self, handle: ExecutionHandle) -> None:
        """Notify external handlers of state change."""
        if self._on_state_change:
            self._on_state_change(handle)
    
    def _notify_progress(self, execution_id: UUID, node_id: str, progress: float) -> None:
        """Notify external handlers of progress update."""
        if self._on_progress:
            self._on_progress(execution_id, node_id, progress)


# Global orchestrator instance
_orchestrator: WorkflowOrchestrator | None = None


def get_orchestrator() -> WorkflowOrchestrator:
    """Get the global WorkflowOrchestrator instance."""
    global _orchestrator
    if _orchestrator is None:
        _orchestrator = WorkflowOrchestrator()
    return _orchestrator
