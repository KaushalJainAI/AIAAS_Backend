import asyncio
import logging
from datetime import datetime
from typing import Any, Dict, List
from uuid import UUID

from django.utils import timezone
from django.db import transaction
from asgiref.sync import sync_to_async

from .models import ExecutionLog, NodeExecutionLog, AuditEntry
from streaming.broadcaster import get_broadcaster
from core.security import get_log_sanitizer

logger = logging.getLogger(__name__)


class ExecutionLogger:
    """
    Writes execution logs to the database with internal buffering for scale.
    
    Provides a clean interface for recording:
    - Workflow execution start/end
    - Individual node execution details
    - Errors and stack traces
    - Audit entries for sensitive actions
    
    Scaling:
    - Buffers NodeExecutionLog entries in memory.
    - Flushes to DB periodically to reduce IOPS.
    """
    
    # Scaling Config
    MAX_BUFFER_SIZE = 5
    FLUSH_INTERVAL_SECONDS = 0.5
    
    _instance = None
    _buffer: Dict[UUID, List[Dict[str, Any]]] = {}
    _flush_tasks: Dict[UUID, asyncio.Task] = {}
    _lock = asyncio.Lock()

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(ExecutionLogger, cls).__new__(cls)
        return cls._instance

    def __init__(self):
        # Initialization is handled via class-level attributes for true singleton behavior
        pass

    async def _get_buffer(self, execution_id: UUID) -> List[Dict[str, Any]]:
        async with self._lock:
            if execution_id not in self._buffer:
                self._buffer[execution_id] = []
            return self._buffer[execution_id]

    def _schedule_flush(self, execution_id: UUID):
        """Schedule a background task to flush logs for this execution."""
        if execution_id in self._flush_tasks and not self._flush_tasks[execution_id].done():
            return
            
        try:
            loop = asyncio.get_running_loop()
            task = loop.create_task(self._periodic_flush(execution_id))
            self._flush_tasks[execution_id] = task
        except RuntimeError:
            # Fallback if no loop is running (rare in this app)
            pass

    async def _periodic_flush(self, execution_id: UUID):
        """Periodically flush logs for an execution."""
        try:
            await asyncio.sleep(self.FLUSH_INTERVAL_SECONDS)
            await self.flush_execution_logs(execution_id)
        except Exception as e:
            logger.error(f"Error in periodic flush for {execution_id}: {e}")
        finally:
            async with self._lock:
                self._flush_tasks.pop(execution_id, None)

    async def flush_execution_logs(self, execution_id: UUID):
        """Atomic batch write of all buffered logs for a specific execution."""
        async with self._lock:
            logs_to_flush = self._buffer.pop(execution_id, [])
            
        if not logs_to_flush:
            return

        try:
            @sync_to_async
            def write_logs_batch():
                with transaction.atomic():
                    try:
                        exec_log = ExecutionLog.objects.get(execution_id=execution_id)
                    except ExecutionLog.DoesNotExist:
                        logger.error(f"Execution {execution_id} lost during log flush.")
                        return

                    for entry in logs_to_flush:
                        op = entry.get('_op')
                        node_id = entry.get('node_id')
                        
                        if op == 'start':
                            NodeExecutionLog.objects.create(
                                execution=exec_log,
                                **{k: v for k, v in entry.items() if not k.startswith('_')}
                            )
                        elif op == 'complete':
                            # Update the latest 'running' log for this specific node
                            NodeExecutionLog.objects.filter(
                                execution=exec_log, 
                                node_id=node_id, 
                                status='running'
                            ).update(**{k: v for k, v in entry.items() if not k.startswith('_')})
                        elif op == 'error':
                            NodeExecutionLog.objects.filter(
                                execution=exec_log, 
                                node_id=node_id
                            ).update(**{k: v for k, v in entry.items() if not k.startswith('_')})
            
            await write_logs_batch()
            logger.debug(f"Scale: Flushed {len(logs_to_flush)} logs for {execution_id}")
        except Exception as e:
            logger.error(f"Critical Log Failure for {execution_id}: {e}")

    def start_execution(
        self,
        workflow,
        user,
        trigger_type: str,
        input_data: dict[str, Any] | None = None,
        parent_execution_id: UUID | None = None,
        nesting_depth: int = 0,
        timeout_budget_ms: int | None = None,
        workflow_snapshot: dict | None = None,
        supervision_level: str = ''
    ) -> ExecutionLog:
        """Create a new execution log entry when workflow starts."""
        from django.db.models import F
        workflow.execution_count = F('execution_count') + 1
        workflow.last_executed_at = timezone.now()
        workflow.save(update_fields=['execution_count', 'last_executed_at'])
        workflow.refresh_from_db()

        exec_log = ExecutionLog.objects.create(
            workflow=workflow,
            user=user,
            status='running',
            trigger_type=trigger_type,
            started_at=timezone.now(),
            input_data=input_data or {},
            parent_execution_id=parent_execution_id,
            nesting_depth=nesting_depth,
            is_subworkflow_execution=bool(parent_execution_id),
            timeout_budget_ms=timeout_budget_ms,
            workflow_snapshot=workflow_snapshot or {},
            supervision_level=supervision_level
        )
        return exec_log
    
    async def start_execution_async(
        self,
        execution_id: UUID,
        workflow_id: int,
        user_id: int,
        trigger_type: str,
        input_data: dict[str, Any] | None = None,
        parent_execution_id: UUID | None = None,
        nesting_depth: int = 0,
        timeout_budget_ms: int | None = None,
        workflow_snapshot: dict | None = None,
        supervision_level: str = ''
    ) -> ExecutionLog:
        """Async version of start_execution."""
        from orchestrator.models import Workflow
        from django.contrib.auth import get_user_model
        
        User = get_user_model()
        workflow = await sync_to_async(Workflow.objects.get)(id=workflow_id)
        user = await sync_to_async(User.objects.get)(id=user_id)
        
        @sync_to_async
        def create_log():
            from django.db.models import F
            workflow.execution_count = F('execution_count') + 1
            workflow.last_executed_at = timezone.now()
            workflow.save(update_fields=['execution_count', 'last_executed_at'])
            
            return ExecutionLog.objects.create(
                execution_id=execution_id,
                workflow=workflow,
                user=user,
                status='running',
                trigger_type=trigger_type,
                started_at=timezone.now(),
                input_data=input_data or {},
                parent_execution_id=parent_execution_id,
                nesting_depth=nesting_depth,
                is_subworkflow_execution=bool(parent_execution_id),
                timeout_budget_ms=timeout_budget_ms,
                workflow_snapshot=workflow_snapshot or {},
                supervision_level=supervision_level
            )
        
        return await create_log()
    
    async def complete_execution(
        self,
        execution_id: UUID,
        output_data: dict[str, Any] | None = None,
        status: str = 'completed',
        error_message: str = '',
        error_node_id: str = ''
    ) -> ExecutionLog | None:
        """Mark execution complete, calculate stats, and flush final logs."""
        await self.flush_execution_logs(execution_id)
        
        try:
            exec_log = await ExecutionLog.objects.aget(execution_id=execution_id)
        except ExecutionLog.DoesNotExist:
            return None
        
        completed_at = timezone.now()
        duration_ms = int((completed_at - exec_log.started_at).total_seconds() * 1000) if exec_log.started_at else 0
        
        # Calculate tokens and nodes from current DB state
        nodes_executed = await exec_log.node_logs.filter(status__in=['completed', 'failed']).acount()
        tokens_used = 0
        async for node_log in exec_log.node_logs.all():
            tokens_used += node_log.output_data.get('tokens_used', 0)
        
        exec_log.status = status
        exec_log.completed_at = completed_at
        exec_log.duration_ms = duration_ms
        exec_log.output_data = output_data or {}
        exec_log.error_message = error_message
        exec_log.error_node_id = error_node_id
        exec_log.nodes_executed = nodes_executed
        exec_log.tokens_used = tokens_used
        await exec_log.asave()

        # Update Workflow stats
        try:
            from django.db.models import F
            @sync_to_async
            def update_workflow_stats():
                wf = exec_log.workflow
                wf.total_executions = F('total_executions') + 1
                if status == 'completed':
                    wf.successful_executions = F('successful_executions') + 1
                if duration_ms:
                    wf.average_duration_ms = duration_ms if not wf.average_duration_ms else (wf.average_duration_ms * 4 + duration_ms) / 5
                wf.save()
            await update_workflow_stats()
        except Exception: pass
        
        # Broadcast
        try:
            broadcaster = get_broadcaster()
            if status == 'completed':
                await broadcaster.workflow_completed(str(execution_id), output_data or {}, duration_ms)
            elif status == 'cancelled':
                await broadcaster.workflow_cancelled(str(execution_id), duration_ms)
            else:
                await broadcaster.workflow_error(str(execution_id), error_message, error_node_id)
        except Exception: pass
        
        return exec_log
    
    async def log_node_start(
        self,
        execution_id: UUID,
        node_id: str,
        node_type: str,
        node_name: str = '',
        input_data: dict[str, Any] | None = None,
        config: dict[str, Any] | None = None
    ) -> None:
        """Buffer node start."""
        log_entry = {
            '_op': 'start',
            'node_id': node_id,
            'node_type': node_type,
            'node_name': node_name or node_id,
            'status': 'running',
            'started_at': timezone.now(),
            'input_data': get_log_sanitizer().sanitize_dict(input_data or {}),
            'config': get_log_sanitizer().sanitize_dict(config or {})
        }
        
        buffer = await self._get_buffer(execution_id)
        buffer.append(log_entry)
        
        if len(buffer) >= self.MAX_BUFFER_SIZE or len(buffer) == 1:
             await self.flush_execution_logs(execution_id)
        else:
             self._schedule_flush(execution_id)


        try:
            await get_broadcaster().node_started(
                str(execution_id), 
                node_id, 
                node_type, 
                node_name or node_id,
                input_data=input_data
            )
        except Exception: pass


    async def log_node_complete(
        self,
        execution_id: UUID,
        node_id: str,
        success: bool,
        output_data: dict[str, Any] | None = None,
        error_message: str = '',
        duration_ms: int = 0,
        warnings: list[Any] | None = None,
        status: str | None = None
    ) -> None:
        """Buffer node completion."""
        if status is None:
            status = 'completed' if success else 'failed'
        log_entry = {
            '_op': 'complete',
            'node_id': node_id,
            'status': status,
            'completed_at': timezone.now(),
            'duration_ms': duration_ms,
            'output_data': get_log_sanitizer().sanitize_dict(output_data or {}),
            'error_message': error_message
        }
        
        buffer = await self._get_buffer(execution_id)
        buffer.append(log_entry)
        
        if len(buffer) >= self.MAX_BUFFER_SIZE or duration_ms < 100:
             await self.flush_execution_logs(execution_id)
        else:
             self._schedule_flush(execution_id)

        try:
            broadcaster = get_broadcaster()
            if success and status == 'completed':
                await broadcaster.node_completed(str(execution_id), node_id, output_data, duration_ms, status=status)
            else:
                # If failed or cancelled, send event with determined status
                if status == 'cancelled':
                     await broadcaster.node_completed(str(execution_id), node_id, output_data, duration_ms, status='cancelled')
                else:
                     await broadcaster.node_error(str(execution_id), node_id, error_message, status=status)
        except Exception: pass

    async def log_node_skip(
        self,
        execution_id: UUID,
        node_id: str,
        reason: str = ''
    ) -> None:
        """Buffer node skip."""
        log_entry = {
            '_op': 'start',
            'node_id': node_id,
            'node_type': 'skipped_node',
            'status': 'skipped',
            'output_data': {'skip_reason': reason}
        }
        buffer = await self._get_buffer(execution_id)
        buffer.append(log_entry)
        await self.flush_execution_logs(execution_id)
        
        # Broadcast skip as a completed event with status='skipped'
        try:
             await get_broadcaster().node_completed(
                 str(execution_id), 
                 node_id, 
                 output_preview={'skip_reason': reason}, 
                 duration_ms=0, 
                 status='skipped'
             )
        except Exception: pass

    async def heartbeat(self, execution_id: UUID) -> None:
        """Bump the updated_at timestamp to indicate the execution is still alive."""
        try:
            # We use update() to avoid full model load/save and be efficient
            await ExecutionLog.objects.filter(execution_id=execution_id).aupdate(updated_at=timezone.now())
        except Exception as e:
            logger.warning(f"Failed to pulse heartbeat for {execution_id}: {e}")

    async def log_error(
        self,
        execution_id: UUID,
        node_id: str,
        error_message: str,
        stack_trace: str = ''
    ) -> None:
        """Buffer error details."""
        log_entry = {
            '_op': 'error',
            'node_id': node_id,
            'status': 'failed',
            'completed_at': timezone.now(),
            'error_message': error_message,
            'error_stack': stack_trace
        }
        buffer = await self._get_buffer(execution_id)
        buffer.append(log_entry)
        await self.flush_execution_logs(execution_id)
        
        try:
            await get_broadcaster().node_error(str(execution_id), node_id, error_message, status='failed')
        except Exception: pass
    
    async def log_orchestrator_thought(
        self,
        execution_id: UUID,
        content: str,
        reasoning: str = '',
        thought_type: str = 'thought',
        node_id: str = 'orchestrator',
        node_name: str = '',
        category: str = 'workflow',
        metadata: dict | None = None,
        model_id: str = '',
        model_name: str = ''
    ) -> None:
        """
        Persist an orchestrator thought immediately to the DB.
        Thoughts are cognitive events and bypass buffering for immediate visibility.
        """
        from django.contrib.auth import get_user_model
        from orchestrator.models import Workflow
        from .models import OrchestratorThought
        
        try:
            exec_log = await ExecutionLog.objects.aget(execution_id=execution_id)
            
            @sync_to_async
            def save_thought():
                return OrchestratorThought.objects.create(
                    user=exec_log.user,
                    execution=exec_log,
                    workflow=exec_log.workflow,
                    node_id=node_id,
                    node_name=node_name or '',
                    category=category,
                    thought_type=thought_type,
                    content=content,
                    reasoning=reasoning,
                    model_id=model_id,
                    model_name=model_name,
                    metadata=metadata or {}
                )
            
            await save_thought()
            
            # Broadcast via standardized SSE
            broadcaster = get_broadcaster()
            if thought_type == 'thinking':
                await broadcaster.orchestrator_thinking(str(execution_id), content, node_id)
            else:
                # Include model info in broadcast if available
                broadcast_content = content
                if model_name:
                    # We can either append to content or send in metadata. 
                    # For now, let's keep SSE content clean and rely on frontend to fetch model info, 
                    # OR include it in metadata if the broadcaster supports it.
                    # Looking at broadcaster.py would be good, but let's assume SSE metadata for now.
                    pass
                await broadcaster.orchestrator_thought(str(execution_id), content, reasoning, node_id)
                
        except ExecutionLog.DoesNotExist:
            logger.warning(f"Failed to log thought for {execution_id}: ExecutionLog not found.")
        except Exception as e:
            logger.error(f"Error logging orchestrator thought for {execution_id}: {e}")

    def create_audit_entry(self, **kwargs) -> AuditEntry:
        """Audit entries are critical; bypass buffer and write immediately."""
        return AuditEntry.objects.create(**kwargs)


_logger_instance: ExecutionLogger | None = None

def get_execution_logger() -> ExecutionLogger:
    global _logger_instance
    if _logger_instance is None:
        _logger_instance = ExecutionLogger()
    return _logger_instance
