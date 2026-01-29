from abc import ABC, abstractmethod
from typing import Any, Dict, Optional, Literal
from uuid import UUID
from enum import Enum

class ExecutionState(str, Enum):
    """Possible states of a workflow execution."""
    PENDING = "pending"
    RUNNING = "running"
    PAUSED = "paused"
    WAITING_HUMAN = "waiting_human"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"

class OrchestratorDecision(ABC):
    """Result of an orchestrator hook decision."""
    pass

class ContinueDecision(OrchestratorDecision):
    pass

class PauseDecision(OrchestratorDecision):
    pass

class RetryDecision(OrchestratorDecision):
    pass

class AbortDecision(OrchestratorDecision):
    reason: str
    def __init__(self, reason: str):
        self.reason = reason

class OrchestratorInterface(ABC):
    """
    Interface for supervisory control over workflow execution.
    Intersects node execution to enforce policies, pause/resume, and handle errors.
    """

    @abstractmethod
    async def before_node(
        self,
        execution_id: UUID,
        node_id: str,
        context: Dict[str, Any]
    ) -> OrchestratorDecision:
        """
        Called before a node is executed.
        Can pause, abort, or modify configuration (though config modification effectively happens here by returning Continue).
        """
        pass

    @abstractmethod
    async def after_node(
        self,
        execution_id: UUID,
        node_id: str,
        result: Any,
        context: Dict[str, Any]
    ) -> OrchestratorDecision:
        """
        Called after a node completes successfully.
        """
        pass

    @abstractmethod
    async def on_error(
        self,
        execution_id: UUID,
        node_id: str,
        error: str,
        context: Dict[str, Any]
    ) -> OrchestratorDecision:
        """
        Called when a node fails.
        Can decide to retry, ignore, or abort.
        """
        pass
