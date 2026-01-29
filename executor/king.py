"""
King Agent Orchestrator

The Supreme Manager that oversees all workflow executions.
It speaks "User Intent" and controls the deterministic ExecutionEngine.

Capabilities:
- Translates natural language to workflow execution
- Manages lifecycle (Start, Stop, Pause, Resume)
- Handles interaction (HITL)
- Supervises multiple engines
"""
import asyncio
import logging
from datetime import datetime
from enum import Enum
from typing import Any, Callable, Dict
from uuid import UUID, uuid4
from dataclasses import dataclass, field

from django.utils import timezone

from compiler.schemas import ExecutionContext
from orchestrator.interface import (
    OrchestratorInterface,
    OrchestratorDecision,
    ContinueDecision,
    PauseDecision,
    AbortDecision,
    ExecutionState
)
from executor.engine import ExecutionEngine
from logs.models import ExecutionLog

logger = logging.getLogger(__name__)


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
    workflow_version_id: int | None = None
    state: ExecutionState = ExecutionState.PENDING
    current_node: str | None = None
    progress: float = 0.0
    started_at: datetime | None = None
    completed_at: datetime | None = None
    error: str | None = None
    pending_hitl: HITLRequest | None = None
    loop_counters: dict[str, int] = field(default_factory=dict)
    parent_execution_id: UUID | None = None


class KingOrchestrator(OrchestratorInterface):
    """
    The King Agent.
    Manages user intent and supervises execution engines.
    """
    
    def __init__(self):
        # State tracking
        self._executions: dict[UUID, ExecutionHandle] = {}
        self._tasks: dict[UUID, asyncio.Task] = {}
        self._pause_events: dict[UUID, asyncio.Event] = {}
        self._hitl_responses: dict[str, asyncio.Queue] = {}
        
        # The Worker
        self.engine = ExecutionEngine(orchestrator=self)
        
        # Callbacks
        self._on_state_change: Callable[[ExecutionHandle], None] | None = None
        self._on_progress: Callable[[UUID, str, float], None] | None = None
        self._on_hitl_request: Callable[[HITLRequest], None] | None = None
    
    def set_callbacks(
        self,
        on_state_change: Callable[[ExecutionHandle], None] | None = None,
        on_progress: Callable[[UUID, str, float], None] | None = None,
        on_hitl_request: Callable[[HITLRequest], None] | None = None,
    ) -> None:
        """Set callback functions for external integration."""
        self._on_state_change = on_state_change
        self._on_progress = on_progress
        self._on_hitl_request = on_hitl_request

    # --- King Agent Capabilities ---

    async def create_workflow_from_intent(self, user_id: int, prompt: str) -> dict:
        """
        (Stub) Ask the AI Planner to generate a workflow from natural language.
        In a real system, this calls the LLM logic (ai_generated.py).
        """
        # Placeholder for AI Planner integration
        # For now, returns a dummy structure or raises not implemented
        # In the future, this calls: AIPlanner.generate(prompt)
        logger.info(f"King Agent receiving intent from user {user_id}: {prompt}")
        return {
            "id": 0, # Ephemeral
            "name": f"Generated: {prompt[:20]}...",
            "nodes": [],
            "edges": []
        }

    async def ask_human(self, execution_id: UUID, question: str, options: list[str] = None) -> Any:
        """
        Pause execution and ask the human a question.
        Returns the human's response.
        """
        handle = self._executions.get(execution_id)
        if not handle:
            return None
            
        request_id = str(uuid4())
        request = HITLRequest(
            id=request_id,
            request_type=HITLRequestType.CLARIFICATION,
            node_id=handle.current_node or "orchestrator",
            message=question,
            options=options or []
        )
        
        handle.state = ExecutionState.WAITING_HUMAN
        handle.pending_hitl = request
        self._notify_state_change(handle)
        
        if self._on_hitl_request:
            self._on_hitl_request(request)
            
        # Wait for response
        response_queue = asyncio.Queue()
        self._hitl_responses[request_id] = response_queue
        
        logger.info(f"King Agent asking human: {question} (req_id={request_id})")
        
        # Block until API/Frontend submits response
        response = await response_queue.get()
        
        handle.state = ExecutionState.RUNNING
        handle.pending_hitl = None
        self._notify_state_change(handle)
        
        del self._hitl_responses[request_id]
        return response

    def submit_human_response(self, request_id: str, response: Any):
        """External API calls this to answer the King."""
        if request_id in self._hitl_responses:
            self._hitl_responses[request_id].put_nowait(response)

    # --- Execution Management ---

    async def start(
        self,
        workflow_json: dict,
        user_id: int,
        input_data: dict[str, Any] | None = None,
        credentials: dict[str, Any] | None = None,
        workflow_version_id: int | None = None,
        parent_execution_id: UUID | None = None,
        nesting_depth: int = 0,
        workflow_chain: list[int] | None = None,
        timeout_budget_ms: int | None = None,
    ) -> ExecutionHandle:
        """Start a new workflow execution via the Engine."""
        execution_id = uuid4()
        workflow_id = workflow_json.get('id', 0)
        
        handle = ExecutionHandle(
            execution_id=execution_id,
            workflow_id=workflow_id,
            user_id=user_id,
            workflow_version_id=workflow_version_id,
            state=ExecutionState.PENDING,
            started_at=timezone.now(),
            parent_execution_id=parent_execution_id,
        )
        self._executions[execution_id] = handle
        
        # Setup pause event
        pause_event = asyncio.Event()
        pause_event.set()
        self._pause_events[execution_id] = pause_event
        
        # Delegate to Engine in a Task
        task = asyncio.create_task(
            self._run_with_engine(
                handle,
                workflow_json,
                input_data,
                credentials,
                parent_execution_id,
                nesting_depth,
                workflow_chain,
                timeout_budget_ms
            )
        )
        self._tasks[execution_id] = task
        
        logger.info(f"King Agent dispatched workflow {workflow_id} (exec_id={execution_id})")
        return handle

    async def _run_with_engine(self, handle, *args):
        """Wrapper to update handle based on Engine result."""
        handle.state = ExecutionState.RUNNING
        self._notify_state_change(handle)
        
        final_state = await self.engine.run_workflow(handle.execution_id, handle.workflow_id, handle.user_id, *args)
        
        handle.state = final_state
        handle.completed_at = timezone.now()
        if final_state == ExecutionState.COMPLETED:
            handle.progress = 100.0
            
        self._notify_state_change(handle)

    # --- OrchestratorInterface Hooks (Supervision) ---
    
    async def before_node(self, execution_id: UUID, node_id: str, context: Dict[str, Any]) -> OrchestratorDecision:
        """King supervises every step."""
        handle = self._executions.get(execution_id)
        if not handle:
            return AbortDecision("Handle lost")

        # Check Pause
        pause_event = self._pause_events.get(execution_id)
        if pause_event and not pause_event.is_set():
            handle.state = ExecutionState.PAUSED
            self._notify_state_change(handle)
            logger.info(f"Execution {execution_id} paused at {node_id}")
            await pause_event.wait()
            handle.state = ExecutionState.RUNNING
            self._notify_state_change(handle)

        if handle.state == ExecutionState.CANCELLED:
             return AbortDecision("Cancelled")

        handle.current_node = node_id
        self._notify_progress(execution_id, node_id, handle.progress)
        
        return ContinueDecision()

    async def after_node(self, execution_id: UUID, node_id: str, result: Any, context: Dict[str, Any]) -> OrchestratorDecision:
        handle = self._executions.get(execution_id)
        if not handle: return AbortDecision("Handle lost")
        
        # Loop tracking
        if result and isinstance(result, dict) and result.get('output_handle') == 'loop':
            handle.loop_counters[node_id] = handle.loop_counters.get(node_id, 0) + 1
            if handle.loop_counters[node_id] > 1000:
                return AbortDecision("Loop limit exceeded")
                
        return ContinueDecision()

    async def on_error(self, execution_id: UUID, node_id: str, error: str, context: Dict[str, Any]) -> OrchestratorDecision:
        """
        Hit an error? The King decides.
        Could ask human here if policy allows.
        """
        logger.error(f"Node {node_id} error: {error}")
        return AbortDecision(error)

    # --- Controls ---

    async def pause(self, execution_id: UUID) -> bool:
        event = self._pause_events.get(execution_id)
        if event:
            event.clear()
            # Handle state update done in hook or here? 
            # Hook will catch it next time node runs.
            # But if we want immediate feedback:
            handle = self._executions.get(execution_id)
            if handle: 
                handle.state = ExecutionState.PAUSED
                self._notify_state_change(handle)
            return True
        return False

    async def resume(self, execution_id: UUID) -> bool:
        event = self._pause_events.get(execution_id)
        if event:
            event.set()
            handle = self._executions.get(execution_id)
            if handle:
                handle.state = ExecutionState.RUNNING
                self._notify_state_change(handle)
            return True
        return False
    
    async def stop(self, execution_id: UUID) -> bool:
        handle = self._executions.get(execution_id)
        if handle:
            handle.state = ExecutionState.CANCELLED
            task = self._tasks.get(execution_id)
            if task: task.cancel()
            self._notify_state_change(handle)
            return True
        return False
        
    def get_status(self, execution_id: UUID) -> ExecutionHandle | None:
        return self._executions.get(execution_id)

    def _notify_state_change(self, handle: ExecutionHandle) -> None:
        if self._on_state_change:
            self._on_state_change(handle)
    
    def _notify_progress(self, execution_id: UUID, node_id: str, progress: float) -> None:
        if self._on_progress:
            self._on_progress(execution_id, node_id, progress)


# Global Instance
_orchestrator: KingOrchestrator | None = None

def get_orchestrator() -> KingOrchestrator:
    global _orchestrator
    if _orchestrator is None:
        _orchestrator = KingOrchestrator()
    return _orchestrator

# Compatibility alias
WorkflowOrchestrator = KingOrchestrator
