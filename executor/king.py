"""
King Agent Orchestrator

The Supreme Manager that oversees all workflow executions.
It speaks "User Intent" and controls the deterministic ExecutionEngine.

Capabilities:
- Translates natural language to workflow execution
- Manages lifecycle (Start, Stop, Pause, Resume)
- Handles interaction (HITL)
- Supervises multiple engines

Security:
- User isolation on all operations
- Authenticated HITL responses
- Timeout-protected blocking operations
"""
import asyncio
import json
import logging
from datetime import datetime
from enum import Enum
from typing import Any, Callable, Dict
from uuid import UUID, uuid4
from dataclasses import dataclass, field
from threading import Lock

from django.utils import timezone

from compiler.schemas import ExecutionContext
from orchestrator.interface import (
    OrchestratorInterface,
    OrchestratorDecision,
    ContinueDecision,
    PauseDecision,
    AbortDecision,
    ExecutionState,
    SupervisionLevel,
)
from executor.engine import ExecutionEngine
from logs.models import ExecutionLog

logger = logging.getLogger(__name__)

# Constants
DEFAULT_HITL_TIMEOUT_SECONDS = 300
MAX_LOOP_COUNT = 1000
EXECUTION_TTL_SECONDS = 3600  # 1 hour


class HITLRequestType(str, Enum):
    """Types of human-in-the-loop requests."""
    APPROVAL = "approval"
    CLARIFICATION = "clarification"
    ERROR_RECOVERY = "error_recovery"
    REVIEW = "review"


class AuthorizationError(Exception):
    """Raised when user is not authorized to perform an action."""
    pass


class HITLTimeoutError(Exception):
    """Raised when HITL request times out."""
    pass


@dataclass
class HITLRequest:
    """A human-in-the-loop interaction request."""
    id: str
    request_type: HITLRequestType
    execution_id: UUID  # Added for auth
    user_id: int  # Added for auth
    node_id: str
    message: str
    options: list[str] = field(default_factory=list)
    timeout_seconds: int = DEFAULT_HITL_TIMEOUT_SECONDS
    created_at: datetime = field(default_factory=datetime.utcnow)
    response: Any = None
    responded_at: datetime | None = None


@dataclass
class ExecutionHandle:
    """Handle for controlling a running workflow.
    
    Includes runtime context for goal-oriented execution control.
    Runtime decisions are based on node outputs and goals, NOT knowledge base.
    """
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
    supervision_level: SupervisionLevel = SupervisionLevel.FULL
    
    # Goal-oriented execution (NEW)
    execution_goal: str = ""  # What user wants to achieve
    goal_conditions: dict = field(default_factory=dict)  # Dynamic conditions
    # e.g., {"min_rows": 100, "max_errors": 5, "timeout_ms": 30000}
    
    # Runtime context (NEW) - used for execution control, NOT knowledge base
    node_outputs: dict[str, Any] = field(default_factory=dict)  # node_id -> output
    runtime_variables: dict[str, Any] = field(default_factory=dict)
    execution_errors: list[dict] = field(default_factory=list)  # Runtime errors
    hitl_decisions: list[dict] = field(default_factory=list)  # Human decisions made
    
    def record_node_output(self, node_id: str, output: Any) -> None:
        """Store a node's output for goal-based decision making."""
        self.node_outputs[node_id] = output
    
    def record_error(self, node_id: str, error: str) -> None:
        """Track an error for dynamic decision making."""
        self.execution_errors.append({
            "node_id": node_id,
            "error": error,
            "timestamp": datetime.utcnow().isoformat()
        })
    
    def check_goal_condition(self, output: Any) -> tuple[bool, str]:
        """
        Check if we should continue based on goal conditions and output.
        
        Returns (should_continue, reason)
        """
        # Check row count conditions
        if "min_rows" in self.goal_conditions:
            if isinstance(output, (list, tuple)):
                if len(output) < self.goal_conditions["min_rows"]:
                    return False, f"Data has {len(output)} rows, minimum is {self.goal_conditions['min_rows']}"
        
        # Check error threshold
        if "max_errors" in self.goal_conditions:
            if len(self.execution_errors) > self.goal_conditions["max_errors"]:
                return False, f"Too many errors ({len(self.execution_errors)})"
        
        # Check output flags
        if isinstance(output, dict):
            if output.get("should_stop"):
                return False, output.get("stop_reason", "Node requested stop")
            if output.get("skip_remaining"):
                return False, "Skip remaining nodes"
        
        return True, "Continue"


class KingOrchestrator(OrchestratorInterface):
    """
    The King Agent - Unified Orchestrator with full LLM capabilities.
    
    Manages user intent and supervises execution engines.
    
    Powers:
    - Generate workflows from natural language (uses knowledge base)
    - Execute workflows (uses runtime context ONLY, not knowledge base)
    - HITL interactions
    - Goal-oriented execution control
    
    Security: All operations require user_id validation.
    """
    
    # Prompts for workflow generation (design-time, uses knowledge base)
    GENERATE_PROMPT = """You are an AI assistant that generates workflow definitions.
Given a natural language description, create a valid workflow JSON.

Available node types:
{node_types}

Output format:
{{
    "name": "Workflow Name",
    "description": "Description",
    "nodes": [
        {{
            "id": "unique_id",
            "type": "node_type",
            "position": {{"x": 100, "y": 100}},
            "data": {{
                "label": "Node Label",
                "config": {{}}
            }}
        }}
    ],
    "edges": [
        {{
            "id": "edge_id",
            "source": "source_node_id",
            "target": "target_node_id",
            "sourceHandle": "output",
            "targetHandle": "input"
        }}
    ]
}}

User request: {description}

Generate ONLY valid JSON, no explanation:"""

    MODIFY_PROMPT = """You are an AI assistant that modifies workflow definitions.
Given an existing workflow and a modification request, update the workflow.

Current workflow:
{workflow_json}

Available node types:
{node_types}

Modification request: {modification}

Output the complete modified workflow as valid JSON only:"""
    
    def __init__(self, llm_type: str = "openrouter", llm_model: str = "google/gemini-2.0-flash-exp:free"):
        # State tracking (thread-safe access)
        self._lock = Lock()
        self._executions: dict[UUID, ExecutionHandle] = {}
        self._tasks: dict[UUID, asyncio.Task] = {}
        self._pause_events: dict[UUID, asyncio.Event] = {}
        self._hitl_requests: dict[str, HITLRequest] = {}  # request_id -> request (for auth)
        self._hitl_responses: dict[str, asyncio.Queue] = {}
        
        # The Worker (execution engine)
        self.engine = ExecutionEngine(orchestrator=self)
        
        # LLM capabilities (from AIWorkflowGenerator)
        self.llm_type = llm_type
        self.llm_model = llm_model
        self._registry = None  # Lazy loaded
        
        # Design-time context cache (for workflow generation ONLY)
        self._design_contexts: dict[int, dict] = {}  # user_id -> context
        
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

    # --- Authorization Helpers ---
    
    def _check_execution_auth(self, execution_id: UUID, user_id: int) -> ExecutionHandle:
        """
        Verify user owns the execution. Raises AuthorizationError if not.
        """
        handle = self._executions.get(execution_id)
        if not handle:
            raise AuthorizationError(f"Execution {execution_id} not found")
        if handle.user_id != user_id:
            logger.warning(f"User {user_id} attempted to access execution {execution_id} owned by {handle.user_id}")
            raise AuthorizationError("Not authorized to access this execution")
        return handle

    # --- King Agent Capabilities (Workflow Generation - uses Knowledge Base) ---
    
    def _get_registry(self):
        """Lazy load node registry."""
        if self._registry is None:
            from nodes.handlers.registry import get_registry
            self._registry = get_registry()
        return self._registry
    
    def _get_node_types_description(self) -> str:
        """Get a summary of available node types for LLM prompt."""
        registry = self._get_registry()
        schemas = registry.get_all_schemas()
        
        descriptions = []
        for schema in schemas:
            fields_list = schema.get('fields', [])
            fields_desc = ", ".join([f.get('id') for f in fields_list[:5]]) if fields_list else "none"
            descriptions.append(
                f"- {schema.get('type')}: {schema.get('name')} ({schema.get('category')}) - fields: {fields_desc}"
            )
        
        return "\n".join(descriptions)
    
    async def _call_llm(
        self,
        prompt: str,
        user_id: int,
        credential_id: str | None = None
    ) -> str:
        """Call LLM to generate response. Uses configured llm_type and llm_model."""
        from compiler.schemas import ExecutionContext
        
        registry = self._get_registry()
        
        if not registry.has_handler(self.llm_type):
            raise ValueError(f"LLM type '{self.llm_type}' not available")
        
        handler = registry.get_handler(self.llm_type)
        
        context = ExecutionContext(
            execution_id=uuid4(),
            user_id=user_id,
            workflow_id=0
        )
        
        config = {
            "prompt": prompt,
            "credential": credential_id,
            "model": self.llm_model,
            "temperature": 0.2,
            "max_tokens": 4000,
        }
        
        result = await handler.execute({}, config, context)
        
        if result.success:
            return result.data.get("content", "")
        else:
            raise Exception(result.error)
    
    def _parse_json_response(self, response: str) -> dict:
        """Parse JSON from LLM response."""
        import json
        response = response.strip()
        
        # Remove markdown code blocks if present
        if response.startswith("```"):
            lines = response.split("\n")
            response = "\n".join(lines[1:-1] if lines[-1] == "```" else lines[1:])
        
        # Find JSON object
        start = response.find("{")
        end = response.rfind("}") + 1
        
        if start >= 0 and end > start:
            json_str = response[start:end]
            return json.loads(json_str)
        
        raise ValueError("No valid JSON found in response")
    
    def _validate_workflow(self, workflow: dict) -> bool:
        """Validate workflow has required structure."""
        if not isinstance(workflow, dict):
            return False
        
        if "nodes" not in workflow or not isinstance(workflow["nodes"], list):
            return False
        
        if "edges" not in workflow or not isinstance(workflow["edges"], list):
            return False
        
        # Validate each node has required fields
        for node in workflow["nodes"]:
            if not all(k in node for k in ["id", "type", "position"]):
                return False
        
        # Validate each edge has required fields
        for edge in workflow["edges"]:
            if not all(k in edge for k in ["source", "target"]):
                return False
        
        return True

    async def create_workflow_from_intent(
        self, 
        user_id: int, 
        prompt: str,
        credential_id: str | None = None
    ) -> dict:
        """
        Generate a workflow from natural language using the LLM.
        
        This method uses the KNOWLEDGE BASE (templates, node registry)
        because it's for DESIGN-TIME workflow creation.
        
        Runtime execution does NOT use this - it uses goal-based control.
        """
        logger.info(f"King Agent generating workflow for user {user_id}: {prompt}")
        
        try:
            # Try finding similar templates first (uses ChromaDB knowledge base)
            strategy, template = await self._decide_generation_strategy(prompt)
            
            if strategy == "reuse" and template:
                return await self._clone_template(template, prompt, user_id, modify=False)
            elif strategy == "clone" and template:
                return await self._clone_template(template, prompt, user_id, modify=True)
            
            # Default: Generate from scratch using LLM
            return await self._generate_workflow_from_scratch(prompt, user_id, credential_id)
            
        except Exception as e:
            logger.error(f"Failed to generate workflow: {e}")
            return {
                "error": f"Generation failed: {str(e)}",
                "name": f"Failed: {prompt[:30]}...",
                "nodes": [],
                "edges": []
            }
    
    async def _decide_generation_strategy(self, requirement: str) -> tuple[str, Any]:
        """Decide whether to create new, clone, or reuse a template."""
        try:
            from templates.services import TemplateService
            tm = TemplateService()
            
            results = await tm.search_templates(requirement, limit=1)
            if not results:
                return "create", None
            
            best = results[0]
            score = best['score']
            template = best['template']
            
            if score > 0.95:
                return "reuse", template
            elif score > 0.6:
                return "clone", template
            else:
                return "create", None
        except Exception:
            return "create", None
    
    async def _clone_template(self, template, requirement: str, user_id: int, modify: bool = True) -> dict:
        """Clone a template and optionally modify it."""
        from templates.models import WorkflowTemplate
        
        tmpl = await WorkflowTemplate.objects.aget(id=template.id if hasattr(template, 'id') else template['id'])
        
        wf_json = {
            "name": f"{tmpl.name} (Copy)",
            "description": tmpl.description,
            "nodes": tmpl.nodes,
            "edges": tmpl.edges,
            "workflow_settings": tmpl.workflow_settings
        }
        
        if modify:
            return await self.modify_workflow(wf_json, requirement, user_id)
        return wf_json
    
    async def _generate_workflow_from_scratch(
        self, 
        description: str, 
        user_id: int, 
        credential_id: str | None
    ) -> dict:
        """Generate a new workflow from scratch using LLM."""
        node_types = self._get_node_types_description()
        prompt = self.GENERATE_PROMPT.format(
            node_types=node_types,
            description=description
        )
        
        response = await self._call_llm(prompt, user_id, credential_id)
        workflow = self._parse_json_response(response)
        
        if not self._validate_workflow(workflow):
            raise ValueError("Invalid workflow structure generated")
        
        return workflow
    
    async def modify_workflow(
        self,
        workflow: dict,
        modification: str,
        user_id: int,
        credential_id: str | None = None,
    ) -> dict:
        """
        Modify an existing workflow based on natural language request.
        
        This uses the KNOWLEDGE BASE (node registry) for design-time modification.
        """
        import json
        
        node_types = self._get_node_types_description()
        prompt = self.MODIFY_PROMPT.format(
            workflow_json=json.dumps(workflow, indent=2),
            node_types=node_types,
            modification=modification
        )
        
        response = await self._call_llm(prompt, user_id, credential_id)
        
        try:
            modified = self._parse_json_response(response)
            
            if not self._validate_workflow(modified):
                raise ValueError("Invalid workflow structure")
            
            return modified
            
        except Exception as e:
            logger.error(f"Failed to modify workflow: {e}")
            return {
                "error": str(e),
                "original": workflow,
            }


    async def ask_human(
        self, 
        execution_id: UUID, 
        question: str, 
        options: list[str] = None,
        timeout_seconds: int = DEFAULT_HITL_TIMEOUT_SECONDS
    ) -> Any:
        """
        Pause execution and ask the human a question.
        Returns the human's response or raises HITLTimeoutError.
        """
        handle = self._executions.get(execution_id)
        if not handle:
            return None
            
        request_id = str(uuid4())
        request = HITLRequest(
            id=request_id,
            request_type=HITLRequestType.CLARIFICATION,
            execution_id=execution_id,
            user_id=handle.user_id,
            node_id=handle.current_node or "orchestrator",
            message=question,
            options=options or [],
            timeout_seconds=timeout_seconds
        )
        
        handle.state = ExecutionState.WAITING_HUMAN
        handle.pending_hitl = request
        self._notify_state_change(handle)
        
        # Store request for auth
        with self._lock:
            self._hitl_requests[request_id] = request
        
        self._safe_callback(self._on_hitl_request, request)
            
        # Wait for response with timeout
        response_queue = asyncio.Queue()
        self._hitl_responses[request_id] = response_queue
        
        logger.info(f"King Agent asking human: {question} (req_id={request_id}, timeout={timeout_seconds}s)")
        
        try:
            response = await asyncio.wait_for(
                response_queue.get(), 
                timeout=timeout_seconds
            )
        except asyncio.TimeoutError:
            logger.warning(f"HITL request {request_id} timed out after {timeout_seconds}s")
            # Cleanup
            with self._lock:
                self._hitl_requests.pop(request_id, None)
            self._hitl_responses.pop(request_id, None)
            handle.pending_hitl = None
            raise HITLTimeoutError(f"Human response timed out after {timeout_seconds} seconds")
        
        # Cleanup
        with self._lock:
            self._hitl_requests.pop(request_id, None)
        self._hitl_responses.pop(request_id, None)
        
        handle.state = ExecutionState.RUNNING
        handle.pending_hitl = None
        self._notify_state_change(handle)
        
        return response

    def submit_human_response(self, request_id: str, response: Any, user_id: int) -> bool:
        """
        External API calls this to answer the King.
        Returns True if successful, False if request not found or unauthorized.
        """
        with self._lock:
            request = self._hitl_requests.get(request_id)
        
        if not request:
            logger.warning(f"HITL response for unknown request: {request_id}")
            return False
        
        # Verify user owns this request
        if request.user_id != user_id:
            logger.warning(f"User {user_id} attempted to respond to HITL request owned by {request.user_id}")
            return False
        
        if request_id in self._hitl_responses:
            self._hitl_responses[request_id].put_nowait(response)
            return True
        return False

    # --- Execution Management (uses RUNTIME context, NOT knowledge base) ---

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
        supervision: str = "full",  # "full", "error_only", or "none"
        # Goal-oriented execution (NEW)
        goal: str = "",  # What the user wants to achieve
        goal_conditions: dict[str, Any] | None = None,  # Dynamic conditions
        # e.g., {"min_rows": 100, "max_errors": 5}
    ) -> ExecutionHandle:
        """Start a new workflow execution via the Engine.
        
        Args:
            supervision: Level of orchestrator supervision.
                - "full": All hooks (before_node, after_node, on_error)
                - "error_only": Only on_error hook (for stable workflows)
                - "none": No hooks (pure engine execution, fastest)
            goal: What the user wants to achieve. Used for dynamic control.
            goal_conditions: Runtime conditions for goal-based decisions.
                - min_rows: Minimum rows required to continue
                - max_errors: Maximum errors before aborting
        
        Note: Execution control uses RUNTIME context (node outputs, goal),
              NOT the knowledge base. Knowledge base is only for generation.
        """
        # Validate supervision level
        try:
            supervision_level = SupervisionLevel(supervision)
        except ValueError:
            supervision_level = SupervisionLevel.FULL
            
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
            supervision_level=supervision_level,
            # Goal-oriented execution
            execution_goal=goal,
            goal_conditions=goal_conditions or {},
        )
        
        with self._lock:
            self._executions[execution_id] = handle
        
        # Setup pause event (set = running, cleared = paused)
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
                timeout_budget_ms,
                supervision_level,  # Pass supervision level
            )
        )
        self._tasks[execution_id] = task
        
        logger.info(f"King Agent dispatched workflow {workflow_id} (exec_id={execution_id})")
        return handle

    async def _run_with_engine(self, handle, workflow_json, input_data, credentials, 
                                parent_execution_id, nesting_depth, workflow_chain, 
                                timeout_budget_ms, supervision_level):
        """Wrapper to update handle based on Engine result."""
        handle.state = ExecutionState.RUNNING
        self._notify_state_change(handle)
        
        try:
            final_state = await self.engine.run_workflow(
                handle.execution_id, 
                handle.workflow_id, 
                handle.user_id,
                workflow_json,
                input_data,
                credentials,
                parent_execution_id,
                nesting_depth,
                workflow_chain,
                timeout_budget_ms,
                supervision_level,  # Pass to engine
            )
            
            handle.state = final_state
            handle.completed_at = timezone.now()
            if final_state == ExecutionState.COMPLETED:
                handle.progress = 100.0
                
            self._notify_state_change(handle)
        finally:
            # Cleanup resources for this execution
            self._cleanup_execution(handle.execution_id)

    def _cleanup_execution(self, execution_id: UUID) -> None:
        """Remove execution-related resources to prevent memory leaks."""
        with self._lock:
            self._tasks.pop(execution_id, None)
            self._pause_events.pop(execution_id, None)
            # Note: We keep _executions for status queries but could add TTL-based eviction

    # --- OrchestratorInterface Hooks (Supervision) ---
    
    async def before_node(self, execution_id: UUID, node_id: str, context: Dict[str, Any]) -> OrchestratorDecision:
        """King supervises every step."""
        handle = self._executions.get(execution_id)
        if not handle:
            return AbortDecision("Handle lost")

        # Check Pause (with race condition protection)
        pause_event = self._pause_events.get(execution_id)
        if pause_event:
            # First check without blocking
            if not pause_event.is_set():
                handle.state = ExecutionState.PAUSED
                self._notify_state_change(handle)
                logger.info(f"Execution {execution_id} paused at {node_id}")
                # Now wait for resume
                await pause_event.wait()
                # Double-check we weren't cancelled during pause
                if handle.state == ExecutionState.CANCELLED:
                    return AbortDecision("Cancelled during pause")
                handle.state = ExecutionState.RUNNING
                self._notify_state_change(handle)

        if handle.state == ExecutionState.CANCELLED:
            return AbortDecision("Cancelled")

        handle.current_node = node_id
        # Calculate progress based on node position (if possible)
        self._notify_progress(execution_id, node_id, handle.progress)
        
        return ContinueDecision()

    async def after_node(
        self, 
        execution_id: UUID, 
        node_id: str, 
        result: Any, 
        context: Dict[str, Any]
    ) -> OrchestratorDecision:
        """
        After each node, use RUNTIME context for goal-based decisions.
        
        This does NOT use the knowledge base - only runtime state:
        - Node outputs
        - Goal conditions  
        - Error thresholds
        """
        handle = self._executions.get(execution_id)
        if not handle: 
            return AbortDecision("Handle lost")
        
        # Store node output in runtime context (NEW)
        handle.record_node_output(node_id, result)
        
        # Loop tracking with branch-aware key
        branch_key = f"{node_id}:{context.get('branch', 'main')}"
        if result and isinstance(result, dict) and result.get('output_handle') == 'loop':
            handle.loop_counters[branch_key] = handle.loop_counters.get(branch_key, 0) + 1
            if handle.loop_counters[branch_key] > MAX_LOOP_COUNT:
                logger.error(f"Loop limit exceeded for {branch_key}")
                return AbortDecision("Loop limit exceeded")
        
        # Goal-based decision making (NEW - uses runtime context only)
        if handle.goal_conditions:
            should_continue, reason = handle.check_goal_condition(result)
            if not should_continue:
                logger.info(f"Goal condition not met: {reason}")
                # Record this decision for transparency
                handle.hitl_decisions.append({
                    "type": "goal_check",
                    "node_id": node_id,
                    "decision": "stop",
                    "reason": reason,
                    "timestamp": datetime.utcnow().isoformat()
                })
                # Could ask human here if supervision level allows
                if handle.supervision_level == SupervisionLevel.FULL:
                    # Optionally ask user before stopping
                    pass  # For now, just stop
                return AbortDecision(f"Goal condition: {reason}")
        
        return ContinueDecision()

    async def on_error(
        self, 
        execution_id: UUID, 
        node_id: str, 
        error: str, 
        context: Dict[str, Any]
    ) -> OrchestratorDecision:
        """
        Hit an error? The King decides using RUNTIME context.
        
        This uses:
        - Error count tracking
        - max_errors goal condition
        - NOT the knowledge base
        """
        handle = self._executions.get(execution_id)
        if not handle:
            return AbortDecision(error)
        
        # Record error in runtime context (NEW)
        handle.record_error(node_id, error)
        logger.error(f"Node {node_id} error: {error} (total errors: {len(handle.execution_errors)})")
        
        # Check max_errors goal condition
        if "max_errors" in handle.goal_conditions:
            max_allowed = handle.goal_conditions["max_errors"]
            if len(handle.execution_errors) > max_allowed:
                return AbortDecision(f"Exceeded max errors ({max_allowed}): {error}")
            else:
                # Under threshold, maybe continue?
                logger.warning(f"Error recorded but under threshold ({len(handle.execution_errors)}/{max_allowed})")
                # For now, still abort on error - but could implement retry logic
        
        # Future: Could ask human for error recovery based on supervision level
        return AbortDecision(error)

    # --- Controls (with user authorization) ---

    async def pause(self, execution_id: UUID, user_id: int) -> bool:
        """Pause execution. Requires authorization."""
        self._check_execution_auth(execution_id, user_id)
        
        event = self._pause_events.get(execution_id)
        if event:
            event.clear()
            handle = self._executions.get(execution_id)
            if handle: 
                handle.state = ExecutionState.PAUSED
                self._notify_state_change(handle)
            return True
        return False

    async def resume(self, execution_id: UUID, user_id: int) -> bool:
        """Resume execution. Requires authorization."""
        self._check_execution_auth(execution_id, user_id)
        
        event = self._pause_events.get(execution_id)
        if event:
            event.set()
            handle = self._executions.get(execution_id)
            if handle:
                handle.state = ExecutionState.RUNNING
                self._notify_state_change(handle)
            return True
        return False
    
    async def stop(self, execution_id: UUID, user_id: int) -> bool:
        """Stop/cancel execution. Requires authorization."""
        self._check_execution_auth(execution_id, user_id)
        
        handle = self._executions.get(execution_id)
        if handle:
            handle.state = ExecutionState.CANCELLED
            task = self._tasks.get(execution_id)
            if task: 
                task.cancel()
            self._notify_state_change(handle)
            # Cleanup resources
            self._cleanup_execution(execution_id)
            return True
        return False
        
    def get_status(self, execution_id: UUID, user_id: int) -> ExecutionHandle | None:
        """Get execution status. Requires authorization."""
        try:
            return self._check_execution_auth(execution_id, user_id)
        except AuthorizationError:
            return None

    # --- Safe Callbacks ---
    
    def _safe_callback(self, callback: Callable | None, *args) -> None:
        """Execute callback safely, catching any exceptions."""
        if callback:
            try:
                callback(*args)
            except Exception as e:
                logger.exception(f"Callback error: {e}")

    def _notify_state_change(self, handle: ExecutionHandle) -> None:
        self._safe_callback(self._on_state_change, handle)
    
    def _notify_progress(self, execution_id: UUID, node_id: str, progress: float) -> None:
        self._safe_callback(self._on_progress, execution_id, node_id, progress)


# Global Instance
_orchestrator: KingOrchestrator | None = None

def get_orchestrator() -> KingOrchestrator:
    global _orchestrator
    if _orchestrator is None:
        _orchestrator = KingOrchestrator()
    return _orchestrator

# Compatibility alias
WorkflowOrchestrator = KingOrchestrator
