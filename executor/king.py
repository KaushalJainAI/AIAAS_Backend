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
from typing import Any, Callable, Dict, Optional
from uuid import UUID, uuid4
from dataclasses import dataclass, field
from threading import RLock

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
from executor.exceptions import (
    AuthorizationError,
    ExecutionNotFoundError,
    HITLTimeoutError,
    LLMProviderError,
    StateConflictError,
)
from executor.hitl import HITLRequest, HITLRequestType
from logs.models import ExecutionLog
from skills.models import Skill
from workflow_backend.thresholds import DEFAULT_HITL_TIMEOUT_SECONDS, MAX_LOOP_COUNT, EXECUTION_TTL_SECONDS

logger = logging.getLogger(__name__)

# Public re-exports — other apps import these from this module. Keep them
# exported so that relocation doesn't break `from executor.king import ...`.
__all__ = [
    "KingOrchestrator",
    "WorkflowOrchestrator",
    "get_orchestrator",
    "ExecutionHandle",
    "HITLRequest",
    "HITLRequestType",
    "AuthorizationError",
    "HITLTimeoutError",
    "StateConflictError",
    "ExecutionNotFoundError",
    "LLMProviderError",
]


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
    workflow_description: str = ""
    execution_context: str = ""  # User-provided context for this specific execution
    node_inputs: dict[str, Any] = field(default_factory=dict)  # node_id -> input
    node_outputs: dict[str, Any] = field(default_factory=dict)  # node_id -> output
    runtime_variables: dict[str, Any] = field(default_factory=dict)
    execution_errors: list[dict] = field(default_factory=list)  # Runtime errors
    hitl_decisions: list[dict] = field(default_factory=list)  # Human decisions made
    workflow_snapshot: dict = field(default_factory=dict) # Full workflow JSON for reference
    
    def record_node_input(self, node_id: str, input_data: Any) -> None:
        """Store a node's input for reasoning context."""
        self.node_inputs[node_id] = input_data

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

Given an existing workflow JSON and a natural-language modification request,
produce a new workflow JSON that applies the change.

Available node types:
{node_types}

Existing workflow:
{workflow_json}

Modification request:
{modification}

Rules:
- Preserve node IDs and positions that don't need to change.
- Only add, remove, or reconfigure nodes strictly required by the request.
- Keep edges consistent (every edge's source/target must exist).
- Output ONLY valid JSON with the same shape as the input (name, description,
  nodes, edges). No prose, no code fences.
"""

    SUPERVISE_PROMPT = """You are the King Orchestrator, the supreme AI supervisor of a workflow execution.
Your job is to provide high-level reasoning and oversight for each step.

Current Execution Goal: {goal}
Current Workflow Intent: {intent}
Current Status: {status}
Current Node: {node_name} ({node_id}) [{node_type}]

Execution Context:
- Completed Steps: {completed_steps}
- Recent Node Results (Context): {data_context}
- Runtime Variables (State): {variables}

{extra_info}

Relevant Skills:
{skills}

# Task:
Analyze the data context above and determine if the workflow is on track to meet the "Execution Goal". 
- If the node output indicates a problem, explain it. 
- If the node output moved the project forward, explain what was achieved.
- Avoid generic phrases like "Proceeding to next step" or "Execution successful".

Provide your response in VALID JSON format with the following fields:
{{
    "thinking": "Your internal, step-by-step technical analysis of the node's output and its impact on the goal.",
    "thought": "A highly useful, insightful summary for the user. Explain the SIGNIFICANCE of this step. Do not artificially limit yourself to 1-2 sentences if the complexity warrants more detail, but stay concise and value-dense."
}}

Output ONLY the JSON object, no other text."""
    
    def __init__(
        self,
        user_id: int | None = None,
        llm_type: str = "openrouter",
        llm_model: str = "google/gemini-2.0-flash-exp:free",
        credential_id: str | None = None,
    ):
        # Identity + LLM config (defaults; may be overridden by user profile).
        self.user_id = user_id
        self.llm_type = llm_type
        self.llm_model = llm_model
        self.credential_id = credential_id
        self.settings_loaded = False

        # Thread-safe state. _lock guards every mutation of the dicts below.
        self._lock = RLock()
        self._executions: dict[UUID, ExecutionHandle] = {}
        self._tasks: dict[UUID, asyncio.Task] = {}
        self._pause_events: dict[UUID, asyncio.Event] = {}
        self._hitl_requests: dict[str, HITLRequest] = {}
        self._hitl_responses: dict[str, asyncio.Queue] = {}

        # The deterministic worker.
        self.engine = ExecutionEngine(orchestrator=self)

        # Background eviction task (started lazily if an event loop exists).
        self._cleanup_task: asyncio.Task | None = None
        self._schedule_cleanup()

        self._registry = None  # Lazy-loaded node registry.
        self._design_context: dict = {}

        # Callbacks (set via set_callbacks).
        self._on_state_change: Callable[[ExecutionHandle], None] | None = None
        self._on_progress: Callable[[UUID, str, float], None] | None = None
        self._on_hitl_request: Callable[[HITLRequest], None] | None = None
    
    def _schedule_cleanup(self):
        """Start background TTL cleanup."""
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                self._cleanup_task = loop.create_task(self._cleanup_loop())
        except Exception as e:
            logger.warning(f"Could not schedule King cleanup: {e}")

    async def _cleanup_loop(self):
        """
        Evict old in-memory executions and reap DB zombies on a 5-minute loop.

        Exits cleanly on CancelledError so orchestrator teardown doesn't spam
        the logs with a spurious error.
        """
        while True:
            try:
                await asyncio.sleep(300)
            except asyncio.CancelledError:
                return
            try:
                now = datetime.utcnow()
                expired = []
                
                with self._lock:
                    for eid, handle in self._executions.items():
                        # If completed/failed and older than TTL
                        is_done = handle.state in (ExecutionState.COMPLETED, ExecutionState.FAILED, ExecutionState.CANCELLED)
                        if is_done and handle.completed_at:
                            age = (now - handle.completed_at.replace(tzinfo=None)).total_seconds()
                            if age > EXECUTION_TTL_SECONDS:
                                expired.append(eid)
                        
                        # Safety: also check if it's stuck running for too long (e.g. 24h)
                        elif handle.started_at:
                            age = (now - handle.started_at.replace(tzinfo=None)).total_seconds()
                            if age > 86400: # 24h
                                expired.append(eid)

                    for eid in expired:
                        self._executions.pop(eid, None)
                        self._tasks.pop(eid, None)
                        self._pause_events.pop(eid, None)
                        
                if expired:
                    logger.info(f"King Cleanup: Evicted {len(expired)} stale executions from memory.")

                # Zombicide: Mark executions as failed in the DB if heartbeat is lost
                try:
                    from datetime import timedelta
                    from logs.models import ExecutionLog
                    from streaming.broadcaster import get_broadcaster
                    
                    zombie_cutoff = timezone.now() - timedelta(minutes=5)
                    # Use aget for async list if possible, or just sync_to_async
                    from asgiref.sync import sync_to_async
                    
                    @sync_to_async
                    def find_and_reap_zombies():
                        # We only reap 'running' or 'pending' ones that are too old
                        # Note: we exclude 'waiting_human' because HITL can take a long time,
                        # BUT the orchestrator should heartbeat while waiting for HITL too.
                        # For now, let's keep it safe and only reap 'running' and 'pending'.
                        zombies = list(ExecutionLog.objects.filter(
                            status__in=['running', 'pending'],
                            updated_at__lt=zombie_cutoff
                        ))
                        
                        count = 0
                        for z in zombies:
                            z.status = 'failed'
                            z.error_message = f"Execution stalled (heartbeat lost for >5m). Last update: {z.updated_at}"
                            z.completed_at = timezone.now()
                            z.save()
                            count += 1
                        return zombies, count

                    zombies_reaped, count = await find_and_reap_zombies()
                    if count > 0:
                        logger.warning(f"Zombicide: Reaped {count} zombie executions from database.")
                        broadcaster = get_broadcaster()
                        for z in zombies_reaped:
                            asyncio.create_task(broadcaster.workflow_error(str(z.execution_id), z.error_message, ""))
                except Exception as e:
                    logger.error(f"Error in Zombicide: {e}")

            except Exception as e:
                logger.error(f"Error in King cleanup loop: {e}")

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
    
    async def _check_execution_auth(self, execution_id: UUID, user_id: int | None = None) -> ExecutionHandle:
        """
        Verify user owns the execution. 
        Falls back to database if handle is not in memory (multi-server support).
        """
        handle = self._executions.get(execution_id)
        
        # Fallback to DB if the handle is not in memory (multi-worker setup).
        if not handle:
            try:
                from logs.models import ExecutionLog
                log = await ExecutionLog.objects.aget(execution_id=execution_id)
                # Coerce the DB string status into the ExecutionState enum so
                # downstream identity checks (`handle.state == ExecutionState.X`)
                # work identically to in-memory handles.
                try:
                    state = ExecutionState(str(log.status))
                except ValueError:
                    state = ExecutionState.PENDING
                handle = ExecutionHandle(
                    execution_id=log.execution_id,
                    workflow_id=log.workflow_id,
                    user_id=log.user_id,
                    state=state,
                    started_at=log.started_at,
                    supervision_level=SupervisionLevel.FULL,
                )
                with self._lock:
                    self._executions[execution_id] = handle
            except Exception:
                raise AuthorizationError(f"Execution {execution_id} not found")
        
        effective_user_id = user_id or self.user_id
        if handle.user_id != effective_user_id:
            logger.warning(f"User {effective_user_id} attempted to access execution {execution_id} owned by {handle.user_id}")
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
    
    async def _broadcast_activity(self, data: dict, execution_id: UUID | None = None):
        """
        Broadcasts an activity notification to the user's channel.
        DEPRECATED: Use get_execution_logger().log_orchestrator_thought instead.
        """
        from logs.logger import get_execution_logger
        if execution_id:
            await get_execution_logger().log_orchestrator_thought(
                execution_id=execution_id,
                content=data.get('thought') or data.get('thinking_message') or 'Updating...',
                reasoning=data.get('thinking', ''),
                thought_type=data.get('type', 'thought'),
                node_id=data.get('node_id') or 'orchestrator'
            )
        elif self.user_id: # Fallback for non-execution specific broadcasts
            from channels.layers import get_channel_layer
            channel_layer = get_channel_layer()
            if channel_layer:
                await channel_layer.group_send(
                    f"hitl_{self.user_id}",
                    {
                        "type": "notification",
                        "data": {
                            "category": "orchestrator_activity",
                            **data
                        }
                    }
                )

    async def _generate_thought(
        self, 
        execution_id: UUID, 
        node_id: str, 
        context: Dict[str, Any], 
        node_type: str = "unknown",
        extra_info: str = ""
    ) -> tuple[str, str | None]:
        """
        Generate a reasoning thought for the current execution state.
        
        Returns:
            (thought_text, error_message)
        """
        handle = self._executions.get(execution_id)
        if not handle:
            return "", "Execution handle not found"

        # Gather context for the prompt
        completed_steps = list(handle.node_outputs.keys())
        variables = handle.runtime_variables
        
        # Look up node name from snapshot
        node_name = node_id
        if handle.workflow_snapshot:
            nodes = handle.workflow_snapshot.get('nodes', [])
            node_data = next((n for n in nodes if n.get('id') == node_id), None)
            if node_data:
                node_name = node_data.get('data', {}).get('label') or node_data.get('data', {}).get('config', {}).get('label') or node_id

        # Gather data context (inputs for current, outputs for others)
        data_ctx = {
            "current_node_input": self._sanitize_data(handle.node_inputs.get(node_id)),
            "recent_outputs": {nid: self._sanitize_data(out) for nid, out in list(handle.node_outputs.items())[-3:]}
        }
        
        # Prepare skills context
        skills_text = "None"
        if hasattr(handle, 'skills_data') and handle.skills_data:
            skills_list = []
            for s in handle.skills_data:
                skills_list.append(f"### {s['title']}\n{s['content']}")
            skills_text = "\n\n".join(skills_list)

        prompt = self.SUPERVISE_PROMPT.format(
            goal=handle.execution_goal or "Complete the workflow",
            intent=handle.workflow_description or "Not specified",
            user_context=handle.execution_context or "None provided",
            status=handle.state,
            node_id=node_id,
            node_name=node_name,
            node_type=node_type, 
            completed_steps=", ".join(completed_steps) if completed_steps else "None",
            data_context=json.dumps(data_ctx, indent=2),
            variables=json.dumps(variables) if variables else "None",
            extra_info=extra_info,
            skills=skills_text
        )

        try:
            # [NEW] Use more descriptive status messages
            status_desc = f"Analyzing {node_type} logic for '{node_name}'..."
            
            # [NEW] Get model info for logging
            model_id, model_name_str = await self._get_model_info(self.llm_model)

            llm_response = await self._call_llm(
                prompt, 
                user_id=handle.user_id,
                thought=status_desc,
                execution_id=execution_id,
                node_id=node_id
            )
            
            # Parse forced JSON reasoning
            try:
                data = self._parse_json_response(llm_response)
                thinking_text = data.get("thinking", "").strip()
                thought_text = data.get("thought", "").strip()
                
                # [NEW] Robust fallback: if summary is missing or empty, use technical thinking
                if not thought_text and thinking_text:
                    thought_text = thinking_text
                elif not thought_text:
                    thought_text = "Analysis complete."
            except:
                # Fallback if model fails to output valid JSON
                thinking_text = ""
                thought_text = llm_response.strip() or "No response generated."

            # Broadcast ACTUAL reasoning
            from logs.logger import get_execution_logger
            if thinking_text:
                await get_execution_logger().log_orchestrator_thought(
                    execution_id=execution_id,
                    content=thinking_text,
                    reasoning=thinking_text,
                    thought_type='thinking',
                    node_id=node_id,
                    node_name=node_name,
                    model_id=model_id,
                    model_name=model_name_str
                )
            
            # Broadcast summary thought
            await get_execution_logger().log_orchestrator_thought(
                execution_id=execution_id,
                content=thought_text,
                reasoning=thinking_text,
                thought_type='thought',
                node_id=node_id,
                node_name=node_name,
                model_id=model_id,
                model_name=model_name_str
            )
            
            # Record it in history formally
            from orchestrator.chat_context import get_thought_history
            history = get_thought_history(str(execution_id))
            history.add_thought(node_id=node_id, thought=thought_text)
            
            return thought_text, None
        except Exception as e:
            error_msg = str(e)
            logger.error(f"Failed to generate thought: {error_msg}")
            
            # Dismiss the "thinking..." state on the frontend if LLM fails
            error_thought = f"Could not generate reasoning due to LLM error: {error_msg}"
            from logs.logger import get_execution_logger
            await get_execution_logger().log_orchestrator_thought(
                execution_id=execution_id,
                content=error_thought,
                reasoning=error_thought,
                thought_type='error',
                node_id=node_id,
                node_name=node_name
            )
            return "", error_msg

    async def ensure_settings_loaded(self):
        """Ensure settings are loaded from user profile (async safe)."""
        if self.settings_loaded or not self.user_id:
            return
        
        from core.models import UserProfile
        from asgiref.sync import sync_to_async
        
        try:
            profile = await sync_to_async(UserProfile.objects.get)(user_id=self.user_id)
            self.llm_type = profile.llm_provider or self.llm_type
            self.llm_model = profile.llm_model or self.llm_model
            if profile.llm_credential_id:
                self.credential_id = str(profile.llm_credential_id)
            self.settings_loaded = True
            logger.debug(f"Orchestrator settings loaded for user {self.user_id}")
        except Exception as e:
            logger.warning(f"Failed to load user settings for {self.user_id}: {e}")
            self.settings_loaded = True # Don't keep retrying if it fails

    async def check_health(self, user_id: int | None = None) -> tuple[bool, str]:
        """
        Verify that the orchestrator LLM is reachable and credentials are valid.
        
        Returns:
            (is_healthy, error_message)
        """
        effective_user_id = user_id or self.user_id
        await self.ensure_settings_loaded()
        
        test_prompt = "Respond with 'ok' and nothing else."
        try:
            # Use a very low token limit for the health check
            registry = self._get_registry()
            if not registry.has_handler(self.llm_type):
                return False, f"LLM provider '{self.llm_type}' not available"
            
            # We don't use _call_llm directly to avoid adding to thought history
            handler = registry.get_handler(self.llm_type)
            
            # Resolve credential (mostly duplicated from _call_llm for health check safety)
            effective_credential_id = self.credential_id
            if not effective_credential_id and effective_user_id and self.llm_type != "ollama":
                from credentials.models import Credential
                from asgiref.sync import sync_to_async
                def find_cred():
                    return Credential.objects.filter(
                        user_id=effective_user_id,
                        credential_type__service_identifier=self.llm_type,
                        is_active=True
                    ).order_by('-last_used_at').first()
                found_cred = await sync_to_async(find_cred)()
                if found_cred:
                    effective_credential_id = str(found_cred.id)

            from compiler.schemas import ExecutionContext
            context = ExecutionContext(execution_id=uuid4(), user_id=effective_user_id, workflow_id=0)
            config = {
                "prompt": test_prompt,
                "credential": effective_credential_id,
                "model": self.llm_model,
                "max_tokens": 10,
                "temperature": 0.0
            }
            
            result = await handler.execute({}, config, context)
            if result.success:
                return True, "Healthy"
            else:
                return False, result.error or "LLM check failed"
        except Exception as e:
            logger.error(f"Orchestrator health check failed: {e}")
            return False, str(e)

    async def _load_user_settings(self):
        """Alias for ensure_settings_loaded."""
        await self.ensure_settings_loaded()

    async def update_settings(self, llm_type: str = None, llm_model: str = None, credential_id: str = None):
        """Update orchestrator settings dynamically and persist to profile."""
        if llm_type:
            self.llm_type = llm_type
        if llm_model:
            self.llm_model = llm_model
        if credential_id:
            self.credential_id = credential_id
        
        self.settings_loaded = True # Mark as loaded since we just updated them
            
        # Persist to profile
        from core.models import UserProfile
        from asgiref.sync import sync_to_async
        
        try:
            def do_save():
                profile, created = UserProfile.objects.get_or_create(user_id=self.user_id)
                if llm_type:
                    profile.llm_provider = llm_type
                if llm_model:
                    profile.llm_model = llm_model
                if credential_id:
                    profile.llm_credential_id = int(credential_id)
                elif credential_id == "": # Specific check for clearing
                    profile.llm_credential_id = None
                profile.save()
                return profile

            await sync_to_async(do_save)()
            logger.info(f"Orchestrator settings persisted for user {self.user_id}: {self.llm_type}/{self.llm_model} (cred={self.credential_id})")
        except Exception as e:
            logger.warning(f"Failed to persist user settings for {self.user_id}: {e}")

    async def _call_llm(
        self,
        prompt: str,
        user_id: int | None = None,
        credential_id: str | None = None,
        thought: str | None = None,
        execution_id: UUID | None = None,
        node_id: str | None = None
    ) -> str:
        """Call LLM to generate response. Uses configured llm_type and llm_model."""
        from compiler.schemas import ExecutionContext
        from orchestrator.chat_context import get_thought_history
        from logs.logger import get_execution_logger
        
        registry = self._get_registry()
        
        if not registry.has_handler(self.llm_type):
            raise ValueError(f"LLM type '{self.llm_type}' not available")
        
        handler = registry.get_handler(self.llm_type)
        
        # Ensure persistent settings are loaded if not already
        await self.ensure_settings_loaded()
        
        effective_user_id = user_id or self.user_id
        
        # 1. Resolve which credential ID to use
        # Priority: 1. Argument, 2. Orchestrator setting, 3. Automatic lookup
        effective_credential_id = credential_id or self.credential_id
        
        if not effective_credential_id and effective_user_id and self.llm_type != "ollama":
            from credentials.models import Credential
            from asgiref.sync import sync_to_async
            try:
                # Look for most recently used active credential for this provider
                def find_cred():
                    return Credential.objects.filter(
                        user_id=effective_user_id,
                        credential_type__service_identifier=self.llm_type,
                        is_active=True
                    ).order_by('-last_used_at').first()
                
                found_cred = await sync_to_async(find_cred)()
                if found_cred:
                    effective_credential_id = str(found_cred.id)
                    logger.info(f"Auto-selected credential {effective_credential_id} for {self.llm_type}")
            except Exception as e:
                logger.warning(f"Failed to auto-select credential: {e}")

        context = ExecutionContext(
            execution_id=execution_id or uuid4(),
            user_id=effective_user_id,
            workflow_id=0
        )
        
        config = {
            "prompt": prompt,
            "credential": effective_credential_id,
            "model": self.llm_model,
            "temperature": 0.2,
            "max_tokens": 4000,
        }
        
        # Record thought if provided
        if thought:
            history = get_thought_history(str(context.execution_id))
            target_node = node_id or "orchestrator"
            
            history.add_thought(node_id=target_node, thought=thought)
            # Broadcast activity as 'thinking' status, not a final thought
            # [NEW] Use 'status' type for transient thinking messages to distinguish from final insights
            asyncio.create_task(get_execution_logger().log_orchestrator_thought(
                execution_id=execution_id,
                content=thought,
                thought_type='status',
                node_id=target_node
            ))
        
        result = await handler.execute({}, config, context)
        
        if result.success:
            # [FIX] Safety check for result.data being None
            data = result.data or {}
            content = data.get("content", "")
            
            # [NEW] Capture model-generated thinking/reasoning if present
            # Handlers will be updated to put this in 'thinking' or search for <think> tags
            captured_thinking = data.get("thinking") or data.get("reasoning")
            if not captured_thinking and "<think>" in content:
                import re
                match = re.search(r"<think>(.*?)</think>", content, re.DOTALL)
                if match:
                    captured_thinking = match.group(1).strip()
                    # Optionally strip from content if the user wants clean output
                    # content = content.replace(match.group(0), "").strip()

            if captured_thinking:
                asyncio.create_task(get_execution_logger().log_orchestrator_thought(
                    execution_id=execution_id,
                    content=captured_thinking,
                    reasoning=captured_thinking,
                    thought_type='thought',
                    node_id=node_id or "orchestrator"
                ))

            return content
        else:
            error_msg = result.error or "Unknown LLM error"
            # Heuristic: pick out connection-class failures so callers can
            # distinguish provider-down from provider-said-something-wrong
            # without resorting to string matching themselves.
            is_connection_error = any(
                phrase in error_msg.lower()
                for phrase in ("connection", "unreachable", "refused", "failed to connect", "not found")
            )

            if is_connection_error:
                asyncio.create_task(get_execution_logger().log_orchestrator_thought(
                    execution_id=execution_id,
                    content=(
                        f"CRITICAL: Failed to connect to {self.llm_type} "
                        f"({self.llm_model}). Ensure the service is reachable."
                    ),
                    reasoning=error_msg,
                    thought_type="error",
                    node_id=node_id or "orchestrator",
                ))

            raise LLMProviderError(error_msg, is_connection_error=is_connection_error)
    
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
        user_id: int | None = None, 
        prompt: str = "",
        credential_id: str | None = None
    ) -> dict:
        """
        Generate a workflow from natural language using the LLM.
        
        This method uses the KNOWLEDGE BASE (templates, node registry)
        because it's for DESIGN-TIME workflow creation.
        
        Runtime execution does NOT use this - it uses goal-based control.
        """
        logger.info(f"King Agent generating workflow for user {user_id}: {prompt}")
        # This broadcast is not tied to an execution_id, so it uses the old method.
        await self._broadcast_activity({"type": "status", "content": f"Processing intent: {prompt[:50]}..."})
        
        try:
            effective_user_id = user_id or self.user_id
            # Try finding similar templates first (uses ChromaDB knowledge base)
            strategy, template = await self._decide_generation_strategy(prompt)
            
            if strategy == "reuse" and template:
                await self._broadcast_activity({"type": "thought", "content": f"Found direct template match: {template['name']}"})
                return await self._clone_template(template, prompt, effective_user_id, modify=False)
            elif strategy == "clone" and template:
                await self._broadcast_activity({"type": "thought", "content": f"Found similar template, adapting it..."})
                return await self._clone_template(template, prompt, effective_user_id, modify=True)
            
            # Default: Generate from scratch using LLM
            await self._broadcast_activity({"type": "thought", "content": f"Generating new workflow from scratch using {self.llm_model}"})
            return await self._generate_workflow_from_scratch(prompt, effective_user_id, credential_id)
            
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
        
        effective_user_id = user_id or self.user_id
        response = await self._call_llm(
            prompt, 
            effective_user_id, 
            credential_id, 
            thought=f"Constructing workflow nodes and edges for: {description[:100]}"
        )
        workflow = self._parse_json_response(response)
        
        if not self._validate_workflow(workflow):
            raise ValueError("Invalid workflow structure generated")
        
        return workflow
    
    async def modify_workflow(
        self,
        workflow: dict,
        modification: str,
        user_id: int | None = None,
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
        
        effective_user_id = user_id or self.user_id
        # This broadcast is not tied to an execution_id, so it uses the old method.
        await self._broadcast_activity({"type": "status", "content": f"Modifying workflow: {modification[:50]}..."})
        # Record thought for modification
        response = await self._call_llm(
            prompt, 
            effective_user_id, 
            credential_id,
            thought=f"Applying modifications: {modification[:100]}"
        )
        
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
            
        # Wait for response with heartbeat pulse
        response_queue = asyncio.Queue()
        self._hitl_responses[request_id] = response_queue
        
        logger.info(f"King Agent asking human: {question} (req_id={request_id}, timeout={timeout_seconds}s)")
        
        from logs.logger import get_execution_logger
        exec_logger = get_execution_logger()
        
        async def heartbeat_pulse():
            while True:
                await asyncio.sleep(60) # Less frequent for HITL
                await exec_logger.heartbeat(execution_id)

        heart_task = asyncio.create_task(heartbeat_pulse())
        
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
        finally:
            heart_task.cancel()
            try:
                await heart_task
            except asyncio.CancelledError:
                pass
        
        # Cleanup
        with self._lock:
            self._hitl_requests.pop(request_id, None)
        self._hitl_responses.pop(request_id, None)
        
        handle.state = ExecutionState.RUNNING
        handle.pending_hitl = None
        self._notify_state_change(handle)
        
        return response

    def submit_human_response(self, request_id: str, response: Any, user_id: int | None = None) -> bool:
        """
        External API calls this to answer the King.
        Returns True if successful, False if request not found or unauthorized.
        """
        with self._lock:
            request = self._hitl_requests.get(request_id)
        
        if not request:
            logger.warning(f"HITL response for unknown request: {request_id}")
            return False
        
        effective_user_id = user_id or self.user_id
        # Verify user owns this request
        if request.user_id != effective_user_id:
            logger.warning(f"User {effective_user_id} attempted to respond to HITL request owned by {request.user_id}")
            return False
        
        if request_id in self._hitl_responses:
            self._hitl_responses[request_id].put_nowait(response)
            return True
        return False

    def respond_to_hitl(self, request_id: str, response: Any, user_id: int | None = None) -> bool:
        """Alias for submit_human_response to match views.py naming."""
        return self.submit_human_response(request_id, response, user_id)

    # --- Execution Management (uses RUNTIME context, NOT knowledge base) ---

    async def start(
        self,
        workflow_json: dict,
        user_id: int | None = None,
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
        context: str = "",  # Added context
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
        
        effective_user_id = user_id or self.user_id
        # Load Skills from database
        skills_data = []
        skill_ids = workflow_json.get('workflow_settings', {}).get('skills', [])
        if skill_ids:
            try:
                # Use sync_to_async or similar if needed, but since we are in start() which is async
                async for skill in Skill.objects.filter(id__in=skill_ids):
                    skills_data.append({
                        'title': skill.title,
                        'content': skill.content
                    })
            except Exception as e:
                logger.error(f"Failed to load skills: {e}")

        handle = ExecutionHandle(
            execution_id=execution_id,
            workflow_id=workflow_id,
            user_id=effective_user_id,
            workflow_version_id=workflow_version_id,
            state=ExecutionState.PENDING,
            started_at=timezone.now(),
            parent_execution_id=parent_execution_id,
            supervision_level=supervision_level,
            # Goal-oriented execution
            execution_goal=goal,
            goal_conditions=goal_conditions or {},
            workflow_description=workflow_json.get('description', ''),
            execution_context=context,
            workflow_snapshot=workflow_json
        )
        # Store for supervision reasoning
        handle.skills_data = skills_data
        
        with self._lock:
            self._executions[execution_id] = handle
        
        # --- FIX: Create ExecutionLog EARLY to capture startup failures ---
        exec_logger = None
        try:
            from logs.logger import ExecutionLogger
            exec_logger = ExecutionLogger()
            await exec_logger.start_execution_async(
                execution_id=execution_id,
                workflow_id=workflow_id,
                user_id=effective_user_id,
                trigger_type="manual", # Default to manual for now
                input_data=input_data,
                nesting_depth=nesting_depth,
                timeout_budget_ms=timeout_budget_ms,
                workflow_snapshot=workflow_json,
                supervision_level=supervision_level
            )
        except Exception as e:
            logger.error(f"Failed to create execution log for {execution_id}: {e}")

        # --- MCP Pre-flight: fail fast if required credentials are missing ---
        try:
            from mcp_integration.workflow_validator import (
                MCPWorkflowValidationError,
                assert_mcp_nodes_valid,
            )
            await assert_mcp_nodes_valid(workflow_json, effective_user_id)
        except MCPWorkflowValidationError as mcp_err:
            error_msg = "MCP configuration error: " + "; ".join(mcp_err.errors)
            logger.warning(
                f"Cannot start workflow {workflow_id} for user {effective_user_id}: {error_msg}"
            )
            if exec_logger:
                await exec_logger.complete_execution(
                    execution_id=execution_id,
                    status='failed',
                    error_message=error_msg,
                    error_node_id="mcp_preflight",
                )
            raise Exception(error_msg)
        except ImportError:
            # mcp_integration is optional; skip silently if not installed.
            pass
        except Exception as e:
            logger.exception(f"MCP pre-flight validation crashed for workflow {workflow_id}: {e}")

        # --- NEW: Orchestrator Health Check ---
        if supervision_level != SupervisionLevel.NONE:
            is_healthy, health_error = await self.check_health(user_id=effective_user_id)
            if not is_healthy:
                error_msg = f"Orchestrator LLM failure: {health_error}. Please check your credentials and connection."
                logger.error(f"Cannot start workflow {workflow_id} for user {effective_user_id}: {error_msg}")
                
                # Log failure to DB if logger is available
                if exec_logger:
                    await exec_logger.complete_execution(
                        execution_id=execution_id,
                        status='failed',
                        error_message=f"Orchestrator Failure: {health_error}",
                        error_node_id="orchestrator"
                    )
                
                # We raise an exception here so the view can return 400/500
                raise Exception(error_msg)

        # Setup pause event (set = running, cleared = paused)
        pause_event = asyncio.Event()
        pause_event.set()
        self._pause_events[execution_id] = pause_event
        
        from django.conf import settings
        if getattr(settings, 'RUN_WORKFLOWS_ASYNC', False):
            # OFFLOAD TO CELERY
            from executor.tasks import run_engine_worker_task
            
            run_engine_worker_task.delay(
                execution_id_str=str(execution_id),
                workflow_id=workflow_id,
                user_id=effective_user_id,
                workflow_json=workflow_json,
                input_data=input_data,
                credentials=credentials,
                parent_execution_id_str=str(parent_execution_id) if parent_execution_id else None,
                nesting_depth=nesting_depth,
                workflow_chain=workflow_chain,
                timeout_budget_ms=timeout_budget_ms,
                supervision=supervision,
            )
            logger.info(f"King Agent offloaded workflow {workflow_id} to Celery worker (exec_id={execution_id})")
        else:
            # RUN LOCALLY
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
            logger.info(f"King Agent dispatched local workflow {workflow_id} (exec_id={execution_id})")
        
        # FIX: Yield to event loop to allow background task to start immediately.
        # This prevents race conditions where instant workflows complete before
        # the API returns, causing frontend state synchronization issues.
        # Increased to 0.1s to ensure completion event happens BEFORE api return for fast flows.
        await asyncio.sleep(0.1)
        
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
                skills=handle.skills_data
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

    async def _get_model_info(self, model_id: str) -> tuple[str, str]:
        """Lookup human-readable model name from technical ID."""
        if not model_id:
            return '', ''
        
        try:
            from nodes.models import AIModel
            model = await AIModel.objects.aget(value=model_id)
            return model_id, model.name
        except Exception:
            # Fallback to model ID if name not found
            return model_id, model_id

    # --- OrchestratorInterface Hooks (Supervision) ---
    def _sanitize_data(self, data: Any) -> Any:
        """Recursively scrub sensitive keys from data before sending to LLM."""
        from core.security import get_log_sanitizer
        if isinstance(data, dict):
            return get_log_sanitizer().sanitize_dict(data)
        elif isinstance(data, list):
            return [self._sanitize_data(i) for i in data]
        return data

    async def before_node(
        self, 
        execution_id: UUID, 
        node_id: str, 
        node_type: str,
        context: Dict[str, Any],
        input_data: Optional[Dict[str, Any]] = None
    ) -> OrchestratorDecision:
        """King supervises every step."""
        handle = self._executions.get(execution_id)
        if not handle:
            return AbortDecision("Handle lost")

        # Record input for reasoning context (NEW)
        if input_data:
            handle.record_node_input(node_id, input_data)

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
        
        # BROADCAST STATUS (Ensure frontend sees it on timeline)
        await self._broadcast_activity({
            "type": "status",
            "content": f"Executing node: {node_id}",
            "node_id": node_id,
            "node_type": node_type,
            "timestamp": datetime.utcnow().isoformat()
        })
        
        # We NO LONGER check full supervision here. User wants thought AFTER execution completes on the node!
        
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
                
                # GENERATE THOUGHT to explain why it stopped
                if handle.supervision_level in (SupervisionLevel.FULL, SupervisionLevel.FAILSAFE):
                    await self._generate_thought(
                        execution_id, 
                        node_id, 
                        context, 
                        node_type="unknown", 
                        extra_info=f"Node output: {self._sanitize_data(result)}\nDecision: Stopping because goal condition not met ({reason})."
                    )

                # Could ask human here if supervision level allows
                return AbortDecision(f"Goal condition: {reason}")
        
        # GENERATE THOUGHT to explain why it continues
        if handle.supervision_level in (SupervisionLevel.FULL, SupervisionLevel.FAILSAFE):
            thought, error = await self._generate_thought(
                execution_id, 
                node_id, 
                context, 
                node_type="unknown", 
                extra_info=f"Node output: {self._sanitize_data(result)}\nDecision: Continuing execution."
            )
            
            # [CRITICAL] If thought generation failed (e.g. 429/404), handle based on level
            if error:
                # [NEW] If level is FAILSAFE, we just log a warning and CONTINUE
                if handle.supervision_level == SupervisionLevel.FAILSAFE:
                    logger.warning(f"Supervision failure (Failsafe mode): {error}. Continuing execution.")
                    # Thought generation already logged the error to the frontend via _generate_thought
                    return ContinueDecision()
                
                # Otherwise, ABORT with the specific error
                error_msg = f"Supervision failure: {error}. Aborting for safety. (Use 'failsafe' mode to ignore)"
                logger.error(f"Execution {execution_id} aborted: {error_msg}")
                
                # Try to log the failure reason
                try:
                    from logs.logger import get_execution_logger
                    await get_execution_logger().log_error(
                        execution_id=execution_id,
                        node_id="orchestrator",
                        error_message=f"Orchestrator Failure mid-workflow during '{node_id}' analysis: {error}"
                    )
                except: pass
                
                return AbortDecision(error_msg)

        return ContinueDecision()

    async def on_error(
        self, 
        execution_id: UUID, 
        node_id: str, 
        node_type: str,
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
        logger.error(f"Node {node_id} ({node_type}) error: {error} (total errors: {len(handle.execution_errors)})")
        
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
        
        # GENERATE THOUGHT (for FULL and ERROR_ONLY supervision)
        if handle.supervision_level in (SupervisionLevel.FULL, SupervisionLevel.ERROR_ONLY, SupervisionLevel.FAILSAFE):
            # Record the error first anyway
            await self._generate_thought(
                execution_id, 
                node_id, 
                context, 
                node_type=node_type,
                extra_info=f"CRITICAL ERROR: {error}\nDecision: Aborting execution."
            )

        return AbortDecision(error)

    # --- Controls (with user authorization) ---

    async def pause(self, execution_id: UUID, user_id: int | None = None) -> bool:
        """Pause execution. Requires authorization."""
        handle = await self._check_execution_auth(execution_id, user_id)
        
        with self._lock:
            event = self._pause_events.get(execution_id)
            task = self._tasks.get(execution_id)
            
            # 1. Check if execution is in a valid state to be paused
            if handle.state in (ExecutionState.COMPLETED, ExecutionState.FAILED, ExecutionState.CANCELLED):
                raise StateConflictError(f"Cannot pause execution in state {handle.state}")
            
            if handle.state == ExecutionState.PAUSED:
                raise StateConflictError("Execution is already paused")

            # 2. Verify Liveness (Ghost check)
            if not task or task.done():
                # Reconcile ghost state
                logger.warning(f"Pause requested for ghost execution {execution_id}. Cleaning up.")
                if task and task.cancelled():
                    handle.state = ExecutionState.CANCELLED
                elif task and task.exception():
                    handle.state = ExecutionState.FAILED
                else:
                    handle.state = ExecutionState.COMPLETED
                self._cleanup_execution(execution_id)
                self._notify_state_change(handle)
                raise StateConflictError(f"Execution has already terminated (reconciled to {handle.state})")

            # 3. Perform Pause
            if event:
                event.clear()
                handle.state = ExecutionState.PAUSED
                self._notify_state_change(handle)
                return True
        
        return False

    async def resume(self, execution_id: UUID, user_id: int | None = None) -> bool:
        """Resume execution. Requires authorization."""
        handle = await self._check_execution_auth(execution_id, user_id)
        
        with self._lock:
            event = self._pause_events.get(execution_id)
            task = self._tasks.get(execution_id)

            if handle.state != ExecutionState.PAUSED:
                raise StateConflictError(f"Cannot resume execution in state {handle.state}")

            # Verify liveness: the task must still be running to resume.
            if not task or task.done():
                logger.warning(
                    f"Resume requested for dead execution {execution_id}. Cleaning up.",
                )
                # A cancelled task reports `.cancelled() == True` only after it
                # has fully unwound. Anything else — including crashed, normal
                # completion, or never-started — is a failure from resume's view.
                handle.state = (
                    ExecutionState.CANCELLED
                    if task and task.cancelled()
                    else ExecutionState.FAILED
                )
                self._cleanup_execution(execution_id)
                self._notify_state_change(handle)
                raise StateConflictError("Execution task is no longer active")

            if event:
                event.set()
                handle.state = ExecutionState.RUNNING
                self._notify_state_change(handle)
                return True
        return False
    
    async def stop(self, execution_id: UUID, user_id: int | None = None) -> bool:
        """Stop/cancel execution. Requires authorization."""
        handle = await self._check_execution_auth(execution_id, user_id)
        
        with self._lock:
            task = self._tasks.get(execution_id)
            
            if handle.state in (ExecutionState.COMPLETED, ExecutionState.FAILED, ExecutionState.CANCELLED):
                 # Not strictly a conflict, but good to report it's already done
                 return True

            handle.state = ExecutionState.CANCELLED
            if task and not task.done(): 
                task.cancel()
            
            self._notify_state_change(handle)
            # Cleanup resources
            self._cleanup_execution(execution_id)
            return True
        
    async def get_status(self, execution_id: UUID, user_id: int | None = None) -> ExecutionHandle | None:
        """Get execution status. Requires authorization."""
        try:
            return await self._check_execution_auth(execution_id, user_id)
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


# User-specific Orchestrator Registry
_user_orchestrators: dict[int, KingOrchestrator] = {}

def get_orchestrator(user_id: int | None = None) -> KingOrchestrator:
    """
    Get a per-user KingOrchestrator instance.
    If no user_id is provided, returns a default/system instance (id=0).
    """
    effective_user_id = user_id if user_id is not None else 0
    
    if effective_user_id not in _user_orchestrators:
        _user_orchestrators[effective_user_id] = KingOrchestrator(user_id=effective_user_id)
    
    return _user_orchestrators[effective_user_id]

# Compatibility alias
WorkflowOrchestrator = KingOrchestrator
