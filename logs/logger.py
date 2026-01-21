"""
Execution Logger - Database Logging for Workflow Executions

Records workflow and node execution history to the database.

Architecture:
- ExecutionLogger: High-level interface for logging operations
- Creates ExecutionLog entries for workflow runs
- Creates NodeExecutionLog entries for each node

Usage:
    logger = ExecutionLogger()
    exec_log = logger.start_execution(workflow, user, 'manual', input_data)
    
    logger.log_node_start(exec_log.execution_id, node_id, node_type, node_name, input_data)
    logger.log_node_complete(exec_log.execution_id, node_id, success, output, error, duration)
    
    logger.complete_execution(exec_log.execution_id, output_data, 'completed')
"""
import logging
from datetime import datetime
from typing import Any
from uuid import UUID

from django.utils import timezone
from django.db import transaction

from .models import ExecutionLog, NodeExecutionLog, AuditEntry

logger = logging.getLogger(__name__)


class ExecutionLogger:
    """
    Writes execution logs to the database.
    
    Provides a clean interface for recording:
    - Workflow execution start/end
    - Individual node execution details
    - Errors and stack traces
    - Audit entries for sensitive actions
    
    Thread-safe: Uses Django's transaction management.
    """
    
    def start_execution(
        self,
        workflow,
        user,
        trigger_type: str,
        input_data: dict[str, Any] | None = None
    ) -> ExecutionLog:
        """
        Create a new execution log entry when workflow starts.
        
        Args:
            workflow: Workflow model instance
            user: User model instance running the workflow
            trigger_type: How it was triggered ('manual', 'schedule', 'webhook', 'api')
            input_data: Initial input data for the workflow
            
        Returns:
            The created ExecutionLog instance
        """
        logger.info(
            f"Starting execution log for workflow {workflow.id} "
            f"(trigger: {trigger_type})"
        )
        
        exec_log = ExecutionLog.objects.create(
            workflow=workflow,
            user=user,
            status='running',
            trigger_type=trigger_type,
            started_at=timezone.now(),
            input_data=input_data or {}
        )
        
        return exec_log
    
    def complete_execution(
        self,
        execution_id: UUID,
        output_data: dict[str, Any] | None = None,
        status: str = 'completed',
        error_message: str = '',
        error_node_id: str = ''
    ) -> ExecutionLog | None:
        """
        Mark an execution as completed (success or failure).
        
        Args:
            execution_id: UUID of the execution
            output_data: Final output from the workflow
            status: Final status ('completed', 'failed', 'cancelled', 'timeout')
            error_message: Error message if failed
            error_node_id: ID of the node that caused failure
            
        Returns:
            Updated ExecutionLog, or None if not found
        """
        try:
            exec_log = ExecutionLog.objects.get(execution_id=execution_id)
        except ExecutionLog.DoesNotExist:
            logger.error(f"Execution log not found: {execution_id}")
            return None
        
        completed_at = timezone.now()
        
        # Calculate duration
        duration_ms = None
        if exec_log.started_at:
            delta = completed_at - exec_log.started_at
            duration_ms = int(delta.total_seconds() * 1000)
        
        # Count executed nodes
        nodes_executed = exec_log.node_logs.filter(
            status__in=['completed', 'failed']
        ).count()
        
        # Sum up tokens used
        tokens_used = sum(
            log.output_data.get('tokens_used', 0)
            for log in exec_log.node_logs.all()
        )
        
        # Update execution log
        exec_log.status = status
        exec_log.completed_at = completed_at
        exec_log.duration_ms = duration_ms
        exec_log.output_data = output_data or {}
        exec_log.error_message = error_message
        exec_log.error_node_id = error_node_id
        exec_log.nodes_executed = nodes_executed
        exec_log.tokens_used = tokens_used
        exec_log.save()
        
        logger.info(
            f"Execution {execution_id} completed: status={status}, "
            f"duration={duration_ms}ms, nodes={nodes_executed}"
        )
        
        return exec_log
    
    def log_node_start(
        self,
        execution_id: UUID,
        node_id: str,
        node_type: str,
        node_name: str = '',
        input_data: dict[str, Any] | None = None,
        config: dict[str, Any] | None = None
    ) -> NodeExecutionLog | None:
        """
        Log the start of a node execution.
        
        Args:
            execution_id: UUID of the parent execution
            node_id: ID of the node being executed
            node_type: Type of node (e.g., 'http_request')
            node_name: Display name of the node
            input_data: Input data received by the node
            config: Node configuration at execution time
            
        Returns:
            Created NodeExecutionLog, or None if execution not found
        """
        try:
            exec_log = ExecutionLog.objects.get(execution_id=execution_id)
        except ExecutionLog.DoesNotExist:
            logger.error(f"Execution log not found: {execution_id}")
            return None
        
        # Determine execution order
        execution_order = exec_log.node_logs.count()
        
        node_log = NodeExecutionLog.objects.create(
            execution=exec_log,
            node_id=node_id,
            node_type=node_type,
            node_name=node_name or node_id,
            status='running',
            execution_order=execution_order,
            started_at=timezone.now(),
            input_data=input_data or {},
            config=config or {}
        )
        
        logger.debug(f"Node {node_id} started (order: {execution_order})")
        
        return node_log
    
    def log_node_complete(
        self,
        execution_id: UUID,
        node_id: str,
        success: bool,
        output_data: dict[str, Any] | None = None,
        error_message: str = '',
        duration_ms: int = 0
    ) -> NodeExecutionLog | None:
        """
        Log the completion of a node execution.
        
        Args:
            execution_id: UUID of the parent execution
            node_id: ID of the completed node
            success: Whether execution succeeded
            output_data: Output produced by the node
            error_message: Error message if failed
            duration_ms: Execution duration in milliseconds
            
        Returns:
            Updated NodeExecutionLog, or None if not found
        """
        try:
            exec_log = ExecutionLog.objects.get(execution_id=execution_id)
            node_log = exec_log.node_logs.get(node_id=node_id)
        except (ExecutionLog.DoesNotExist, NodeExecutionLog.DoesNotExist):
            logger.error(
                f"Node log not found: execution={execution_id}, node={node_id}"
            )
            return None
        
        node_log.status = 'completed' if success else 'failed'
        node_log.completed_at = timezone.now()
        node_log.duration_ms = duration_ms
        node_log.output_data = output_data or {}
        node_log.error_message = error_message
        node_log.save()
        
        log_level = logging.DEBUG if success else logging.WARNING
        logger.log(
            log_level,
            f"Node {node_id} {'completed' if success else 'failed'}: "
            f"duration={duration_ms}ms"
        )
        
        return node_log
    
    def log_node_skip(
        self,
        execution_id: UUID,
        node_id: str,
        reason: str = ''
    ) -> NodeExecutionLog | None:
        """
        Log that a node was skipped (e.g., conditional branch not taken).
        
        Args:
            execution_id: UUID of the parent execution
            node_id: ID of the skipped node
            reason: Reason for skipping
            
        Returns:
            Created NodeExecutionLog, or None if execution not found
        """
        try:
            exec_log = ExecutionLog.objects.get(execution_id=execution_id)
        except ExecutionLog.DoesNotExist:
            logger.error(f"Execution log not found: {execution_id}")
            return None
        
        node_log = NodeExecutionLog.objects.create(
            execution=exec_log,
            node_id=node_id,
            node_type='unknown',  # We don't know type for skipped nodes
            status='skipped',
            execution_order=exec_log.node_logs.count(),
            output_data={'skip_reason': reason}
        )
        
        logger.debug(f"Node {node_id} skipped: {reason}")
        
        return node_log
    
    def log_error(
        self,
        execution_id: UUID,
        node_id: str,
        error_message: str,
        stack_trace: str = ''
    ) -> NodeExecutionLog | None:
        """
        Log an error for a node execution.
        
        Args:
            execution_id: UUID of the parent execution
            node_id: ID of the failed node
            error_message: Human-readable error message
            stack_trace: Full stack trace if available
            
        Returns:
            Updated NodeExecutionLog, or None if not found
        """
        try:
            exec_log = ExecutionLog.objects.get(execution_id=execution_id)
            node_log = exec_log.node_logs.get(node_id=node_id)
        except (ExecutionLog.DoesNotExist, NodeExecutionLog.DoesNotExist):
            logger.error(f"Cannot log error - node log not found: {node_id}")
            return None
        
        node_log.status = 'failed'
        node_log.completed_at = timezone.now()
        node_log.error_message = error_message
        node_log.error_stack = stack_trace
        node_log.save()
        
        logger.error(f"Node {node_id} error: {error_message}")
        
        return node_log
    
    def create_audit_entry(
        self,
        user,
        action_type: str,
        request_details: dict[str, Any],
        workflow=None,
        execution: ExecutionLog | None = None,
        node_id: str = '',
        response: dict[str, Any] | None = None,
        ip_address: str = '',
        user_agent: str = ''
    ) -> AuditEntry:
        """
        Create an audit trail entry for sensitive actions.
        
        Args:
            user: User who performed the action
            action_type: Type of action (approval, credential_access, etc.)
            request_details: Details of what was requested
            workflow: Related workflow (optional)
            execution: Related execution (optional)
            node_id: Related node ID (optional)
            response: User's response (optional)
            ip_address: Client IP address
            user_agent: Client user agent
            
        Returns:
            Created AuditEntry
        """
        entry = AuditEntry.objects.create(
            user=user,
            workflow=workflow,
            execution=execution,
            node_id=node_id,
            action_type=action_type,
            request_details=request_details,
            response=response or {},
            ip_address=ip_address or None,
            user_agent=user_agent
        )
        
        logger.info(f"Audit entry created: {action_type} by user {user.id}")
        
        return entry


# Singleton instance for convenience
_logger_instance: ExecutionLogger | None = None


def get_execution_logger() -> ExecutionLogger:
    """Get the global ExecutionLogger instance."""
    global _logger_instance
    if _logger_instance is None:
        _logger_instance = ExecutionLogger()
    return _logger_instance
