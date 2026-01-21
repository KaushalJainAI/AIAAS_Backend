"""
Approval Gates - Human-in-the-Loop Approval System

Provides blocking approval gates with timeouts and notifications.
"""
import asyncio
import logging
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID, uuid4
from enum import Enum

from django.utils import timezone

logger = logging.getLogger(__name__)


class ApprovalAction(str, Enum):
    """Possible approval actions."""
    APPROVE = "approve"
    REJECT = "reject"
    SKIP = "skip"
    RETRY = "retry"
    TIMEOUT = "timeout"


class ApprovalGate:
    """
    Blocking approval gate for HITL workflows.
    
    Usage:
        gate = ApprovalGate(
            execution_id=exec_id,
            node_id="node_1",
            user_id=1,
            message="Approve sending email to 100 recipients?"
        )
        
        result = await gate.wait_for_approval(timeout=300)
        
        if result.approved:
            # Continue
        else:
            # Handle rejection
    """
    
    def __init__(
        self,
        execution_id: UUID,
        node_id: str,
        user_id: int,
        message: str,
        title: str = "Approval Required",
        options: list[str] | None = None,
        context_data: dict | None = None,
        timeout_seconds: int = 300,
        auto_action: ApprovalAction = ApprovalAction.TIMEOUT,
    ):
        self.request_id = uuid4()
        self.execution_id = execution_id
        self.node_id = node_id
        self.user_id = user_id
        self.message = message
        self.title = title
        self.options = options or ["approve", "reject"]
        self.context_data = context_data or {}
        self.timeout_seconds = timeout_seconds
        self.auto_action = auto_action
        
        self.created_at = datetime.utcnow()
        self.response: dict | None = None
        self.responded_at: datetime | None = None
        
        # Response queue for async waiting
        self._response_queue: asyncio.Queue = asyncio.Queue()
    
    async def wait_for_approval(self, timeout: int | None = None) -> 'ApprovalResult':
        """
        Wait for user approval with timeout.
        
        Args:
            timeout: Override timeout (uses self.timeout_seconds if None)
            
        Returns:
            ApprovalResult with action and response data
        """
        timeout = timeout or self.timeout_seconds
        
        # Save to database for persistence
        await self._save_to_database()
        
        # Send notification
        await self._send_notification()
        
        try:
            # Wait for response
            response = await asyncio.wait_for(
                self._response_queue.get(),
                timeout=timeout
            )
            
            self.response = response
            self.responded_at = datetime.utcnow()
            
            # Update database
            await self._update_database(response)
            
            action = ApprovalAction(response.get('action', 'approve'))
            
            return ApprovalResult(
                approved=action == ApprovalAction.APPROVE,
                action=action,
                response=response,
                response_time_ms=int((self.responded_at - self.created_at).total_seconds() * 1000),
            )
            
        except asyncio.TimeoutError:
            logger.warning(f"Approval gate {self.request_id} timed out")
            
            # Update database with timeout
            await self._update_database({'action': 'timeout'}, status='timeout')
            
            return ApprovalResult(
                approved=self.auto_action == ApprovalAction.APPROVE,
                action=self.auto_action,
                response={'action': self.auto_action.value, 'reason': 'timeout'},
                timed_out=True,
            )
    
    async def submit_response(self, response: dict) -> None:
        """Submit a response to this approval gate."""
        await self._response_queue.put(response)
    
    async def _save_to_database(self) -> None:
        """Save approval request to database."""
        from asgiref.sync import sync_to_async
        from orchestrator.models import HITLRequest
        from logs.models import ExecutionLog
        
        @sync_to_async
        def save():
            try:
                execution = ExecutionLog.objects.get(execution_id=self.execution_id)
            except ExecutionLog.DoesNotExist:
                execution = None
            
            HITLRequest.objects.create(
                request_id=self.request_id,
                execution=execution,
                user_id=self.user_id,
                node_id=self.node_id,
                request_type='approval',
                title=self.title,
                message=self.message,
                options=self.options,
                context_data=self.context_data,
                timeout_seconds=self.timeout_seconds,
                auto_action=self.auto_action.value,
            )
        
        await save()
    
    async def _update_database(self, response: dict, status: str = 'approved') -> None:
        """Update database with response."""
        from asgiref.sync import sync_to_async
        from orchestrator.models import HITLRequest
        
        @sync_to_async
        def update():
            try:
                request = HITLRequest.objects.get(request_id=self.request_id)
                
                action = response.get('action', 'approve')
                if action in ('approve', 'approved'):
                    request.status = 'approved'
                elif action in ('reject', 'rejected'):
                    request.status = 'rejected'
                elif action == 'timeout':
                    request.status = 'timeout'
                else:
                    request.status = status
                
                request.response = response
                request.responded_at = timezone.now()
                request.save()
                
            except HITLRequest.DoesNotExist:
                pass
        
        await update()
    
    async def _send_notification(self) -> None:
        """Send notification to user."""
        from streaming.consumers import send_hitl_request_to_user
        
        request_data = {
            'request_id': str(self.request_id),
            'type': 'approval',
            'title': self.title,
            'message': self.message,
            'options': self.options,
            'node_id': self.node_id,
            'execution_id': str(self.execution_id),
            'timeout_seconds': self.timeout_seconds,
            'created_at': self.created_at.isoformat(),
        }
        
        try:
            await send_hitl_request_to_user(self.user_id, request_data)
        except Exception as e:
            logger.error(f"Failed to send HITL notification: {e}")


class ApprovalResult:
    """Result of an approval gate."""
    
    def __init__(
        self,
        approved: bool,
        action: ApprovalAction,
        response: dict,
        response_time_ms: int = 0,
        timed_out: bool = False,
    ):
        self.approved = approved
        self.action = action
        self.response = response
        self.response_time_ms = response_time_ms
        self.timed_out = timed_out
    
    @property
    def rejected(self) -> bool:
        return self.action == ApprovalAction.REJECT
    
    @property
    def skipped(self) -> bool:
        return self.action == ApprovalAction.SKIP


class ApprovalGateManager:
    """
    Manages active approval gates.
    
    Allows external systems to submit responses to pending gates.
    """
    
    def __init__(self):
        self._gates: dict[UUID, ApprovalGate] = {}
    
    def register(self, gate: ApprovalGate) -> None:
        """Register an approval gate."""
        self._gates[gate.request_id] = gate
    
    def unregister(self, request_id: UUID) -> None:
        """Unregister an approval gate."""
        self._gates.pop(request_id, None)
    
    async def submit_response(self, request_id: UUID, response: dict) -> bool:
        """
        Submit a response to a pending approval gate.
        
        Returns True if gate was found and response submitted.
        """
        gate = self._gates.get(request_id)
        if gate:
            await gate.submit_response(response)
            return True
        return False
    
    def get_pending_for_user(self, user_id: int) -> list[ApprovalGate]:
        """Get all pending gates for a user."""
        return [
            gate for gate in self._gates.values()
            if gate.user_id == user_id and gate.response is None
        ]


# Global instance
_gate_manager: ApprovalGateManager | None = None


def get_gate_manager() -> ApprovalGateManager:
    """Get global approval gate manager."""
    global _gate_manager
    if _gate_manager is None:
        _gate_manager = ApprovalGateManager()
    return _gate_manager


async def require_approval(
    execution_id: UUID,
    node_id: str,
    user_id: int,
    message: str,
    **kwargs
) -> ApprovalResult:
    """
    Convenience function to require approval before continuing.
    
    Usage:
        result = await require_approval(
            execution_id=exec_id,
            node_id="send_email",
            user_id=1,
            message="Send email to 100 recipients?"
        )
        
        if not result.approved:
            raise ExecutionCancelled("User rejected")
    """
    gate = ApprovalGate(
        execution_id=execution_id,
        node_id=node_id,
        user_id=user_id,
        message=message,
        **kwargs
    )
    
    manager = get_gate_manager()
    manager.register(gate)
    
    try:
        return await gate.wait_for_approval()
    finally:
        manager.unregister(gate.request_id)
