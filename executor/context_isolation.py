"""
Execution Context Isolation and Thread Safety

Provides per-request execution context isolation and thread-local storage.
"""
import asyncio
import contextvars
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

# Context variables for thread-safe per-request state
_current_user_id: contextvars.ContextVar[int | None] = contextvars.ContextVar('current_user_id', default=None)
_current_execution_id: contextvars.ContextVar[UUID | None] = contextvars.ContextVar('current_execution_id', default=None)
_current_workflow_id: contextvars.ContextVar[int | None] = contextvars.ContextVar('current_workflow_id', default=None)
_request_context: contextvars.ContextVar[dict] = contextvars.ContextVar('request_context', default={})


@dataclass
class IsolatedExecutionContext:
    """
    Isolated execution context for a single workflow run.
    
    Provides:
    - Complete isolation of state between executions
    - Thread-safe variable access via contextvars
    - No shared mutable state between users
    """
    execution_id: UUID
    user_id: int
    workflow_id: int
    
    # Per-execution state (not shared)
    node_outputs: dict[str, Any] = field(default_factory=dict)
    variables: dict[str, Any] = field(default_factory=dict)
    errors: list[dict] = field(default_factory=list)
    
    # Metadata
    started_at: datetime = field(default_factory=datetime.utcnow)
    current_node_id: str | None = None
    
    # Credentials (loaded on-demand, per-user isolation)
    _credentials: dict[str, Any] = field(default_factory=dict)
    
    def set_output(self, node_id: str, output: Any) -> None:
        """Set output for a node (isolated to this context)."""
        self.node_outputs[node_id] = output
    
    def get_output(self, node_id: str) -> Any:
        """Get output from a node (isolated to this context)."""
        return self.node_outputs.get(node_id)
    
    def set_variable(self, name: str, value: Any) -> None:
        """Set a workflow variable (isolated to this context)."""
        self.variables[name] = value
    
    def get_variable(self, name: str, default: Any = None) -> Any:
        """Get a workflow variable (isolated to this context)."""
        return self.variables.get(name, default)
    
    def add_error(self, node_id: str, error: str, stack: str = "") -> None:
        """Record an error (isolated to this context)."""
        self.errors.append({
            "node_id": node_id,
            "error": error,
            "stack": stack,
            "timestamp": datetime.utcnow().isoformat(),
        })
    
    async def get_credential(self, credential_id: str) -> dict[str, Any]:
        """
        Get credential (with per-user isolation).
        
        Credentials are fetched and cached per-execution, ensuring
        users can only access their own credentials.
        """
        if credential_id in self._credentials:
            return self._credentials[credential_id]
        
        # Fetch with user isolation
        from credentials.manager import get_credential_manager
        
        manager = get_credential_manager()
        cred = await manager.get_credential(credential_id, self.user_id)
        
        if cred:
            self._credentials[credential_id] = cred
            return cred
        
        return {}


class ContextManager:
    """
    Manages execution contexts with proper isolation.
    
    Usage:
        async with get_context_manager().create_context(user_id, workflow_id) as ctx:
            # All operations within this block use isolated context
            ctx.set_variable("foo", "bar")
    """
    
    def __init__(self):
        # Active contexts (keyed by execution_id)
        self._contexts: dict[UUID, IsolatedExecutionContext] = {}
    
    @asynccontextmanager
    async def create_context(
        self,
        user_id: int,
        workflow_id: int,
        execution_id: UUID | None = None,
    ):
        """
        Create a new isolated execution context.
        
        This is an async context manager that sets up thread-local state
        and ensures cleanup on exit.
        """
        exec_id = execution_id or uuid4()
        
        ctx = IsolatedExecutionContext(
            execution_id=exec_id,
            user_id=user_id,
            workflow_id=workflow_id,
        )
        
        # Store in active contexts
        self._contexts[exec_id] = ctx
        
        # Set context variables
        token_user = _current_user_id.set(user_id)
        token_exec = _current_execution_id.set(exec_id)
        token_workflow = _current_workflow_id.set(workflow_id)
        
        try:
            yield ctx
        finally:
            # Cleanup context variables
            _current_user_id.reset(token_user)
            _current_execution_id.reset(token_exec)
            _current_workflow_id.reset(token_workflow)
            
            # Remove from active contexts
            self._contexts.pop(exec_id, None)
    
    def get_context(self, execution_id: UUID) -> IsolatedExecutionContext | None:
        """Get an active context by ID."""
        return self._contexts.get(execution_id)
    
    def get_current_user_id(self) -> int | None:
        """Get the current user ID from context."""
        return _current_user_id.get()
    
    def get_current_execution_id(self) -> UUID | None:
        """Get the current execution ID from context."""
        return _current_execution_id.get()
    
    def get_current_workflow_id(self) -> int | None:
        """Get the current workflow ID from context."""
        return _current_workflow_id.get()
    
    def set_request_context(self, key: str, value: Any) -> None:
        """Set a value in the request context."""
        ctx = _request_context.get().copy()
        ctx[key] = value
        _request_context.set(ctx)
    
    def get_request_context(self, key: str, default: Any = None) -> Any:
        """Get a value from the request context."""
        return _request_context.get().get(key, default)


# Global instance
_context_manager: ContextManager | None = None


def get_context_manager() -> ContextManager:
    """Get global context manager."""
    global _context_manager
    if _context_manager is None:
        _context_manager = ContextManager()
    return _context_manager


# Convenience functions
def current_user_id() -> int | None:
    """Get current user ID from thread-local context."""
    return _current_user_id.get()


def current_execution_id() -> UUID | None:
    """Get current execution ID from thread-local context."""
    return _current_execution_id.get()
