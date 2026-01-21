"""
Celery Tasks for Workflow Execution

Asynchronous task definitions for:
- Workflow execution
- Document processing and indexing
- Scheduled workflows
- Cleanup tasks
"""
import logging
from datetime import timedelta
from uuid import UUID

from celery import shared_task
from django.utils import timezone

logger = logging.getLogger(__name__)


# ======================== Workflow Execution Tasks ========================

@shared_task(bind=True, max_retries=3, default_retry_delay=60)
def execute_workflow_async(
    self,
    workflow_id: int,
    user_id: int,
    input_data: dict | None = None,
):
    """
    Execute a workflow asynchronously.
    
    Args:
        workflow_id: Workflow to execute
        user_id: User triggering the execution
        input_data: Initial input data
    """
    import asyncio
    from executor.orchestrator import get_orchestrator
    from orchestrator.models import Workflow
    
    try:
        workflow = Workflow.objects.get(id=workflow_id, user_id=user_id)
    except Workflow.DoesNotExist:
        logger.error(f"Workflow {workflow_id} not found")
        return {"error": "Workflow not found"}
    
    workflow_json = {
        'id': workflow.id,
        'nodes': workflow.nodes,
        'edges': workflow.edges,
        'settings': workflow.workflow_settings,
    }
    
    async def run():
        orchestrator = get_orchestrator()
        handle = await orchestrator.start(
            workflow_json=workflow_json,
            user_id=user_id,
            input_data=input_data or {},
        )
        
        # Wait for completion (with timeout)
        import asyncio
        for _ in range(300):  # 5 minute max
            if handle.completed_at:
                break
            await asyncio.sleep(1)
        
        return {
            "execution_id": str(handle.execution_id),
            "state": handle.state.value,
            "error": handle.error,
        }
    
    try:
        result = asyncio.run(run())
        return result
    except Exception as e:
        logger.exception(f"Workflow execution failed: {e}")
        self.retry(exc=e)


@shared_task
def execute_scheduled_workflows():
    """
    Check and execute workflows that are due for scheduled execution.
    Called periodically via Celery Beat.
    """
    from orchestrator.models import Workflow
    
    # Find workflows with active schedules
    # TODO: Implement schedule parsing and matching
    logger.info("Checking scheduled workflows...")
    
    # Placeholder for schedule implementation
    return {"checked": 0, "executed": 0}


# ======================== Document Processing Tasks ========================

@shared_task(bind=True, max_retries=2)
def index_document_async(self, document_id: int):
    """
    Index a document for RAG asynchronously.
    
    Args:
        document_id: Document to index
    """
    import asyncio
    from inference.models import Document
    from inference.engine import get_knowledge_base
    
    try:
        doc = Document.objects.get(id=document_id)
    except Document.DoesNotExist:
        logger.error(f"Document {document_id} not found")
        return {"error": "Document not found"}
    
    doc.status = 'processing'
    doc.save()
    
    async def index():
        kb = get_knowledge_base()
        await kb.initialize()
        
        content = doc.content_text or doc.file.read().decode('utf-8', errors='ignore')
        chunks = await kb.add_document(doc.id, content, {'name': doc.name})
        
        doc.chunk_count = len(chunks)
        doc.status = 'indexed'
        doc.indexed_at = timezone.now()
        doc.save()
        
        return len(chunks)
    
    try:
        chunk_count = asyncio.run(index())
        return {"document_id": document_id, "chunks": chunk_count}
    except Exception as e:
        doc.status = 'failed'
        doc.error_message = str(e)
        doc.save()
        logger.exception(f"Document indexing failed: {e}")
        self.retry(exc=e)


# ======================== Cleanup Tasks ========================

@shared_task
def cleanup_old_executions(days: int = 30):
    """
    Clean up old execution logs.
    
    Args:
        days: Delete executions older than this many days
    """
    from logs.models import ExecutionLog, NodeExecutionLog
    
    cutoff = timezone.now() - timedelta(days=days)
    
    # Delete old node logs first (due to FK constraint)
    node_deleted = NodeExecutionLog.objects.filter(
        execution__created_at__lt=cutoff
    ).delete()[0]
    
    # Delete old execution logs
    exec_deleted = ExecutionLog.objects.filter(
        created_at__lt=cutoff
    ).delete()[0]
    
    logger.info(f"Cleanup: deleted {exec_deleted} executions, {node_deleted} node logs")
    
    return {
        "executions_deleted": exec_deleted,
        "node_logs_deleted": node_deleted,
    }


@shared_task
def cleanup_expired_hitl_requests():
    """
    Timeout expired HITL requests.
    """
    from orchestrator.models import HITLRequest
    
    # Find pending requests past their timeout
    pending = HITLRequest.objects.filter(status='pending')
    expired_count = 0
    
    for request in pending:
        timeout = request.timeout_seconds or 300
        expiry = request.created_at + timedelta(seconds=timeout)
        
        if timezone.now() > expiry:
            request.status = 'timeout'
            request.save()
            expired_count += 1
    
    if expired_count:
        logger.info(f"Expired {expired_count} HITL requests")
    
    return {"expired": expired_count}


@shared_task
def refresh_oauth_tokens():
    """
    Refresh OAuth tokens that are about to expire.
    """
    import asyncio
    from credentials.models import Credential
    from credentials.manager import get_credential_manager
    
    # Find OAuth credentials expiring in next 10 minutes
    expiry_threshold = timezone.now() + timedelta(minutes=10)
    
    expiring = Credential.objects.filter(
        is_active=True,
        credential_type__auth_method='oauth2',
        token_expires_at__lt=expiry_threshold,
        token_expires_at__gt=timezone.now(),
    )
    
    refreshed = 0
    manager = get_credential_manager()
    
    for cred in expiring:
        try:
            asyncio.run(manager.refresh_oauth_token(cred))
            refreshed += 1
        except Exception as e:
            logger.error(f"Failed to refresh token for credential {cred.id}: {e}")
    
    if refreshed:
        logger.info(f"Refreshed {refreshed} OAuth tokens")
    
    return {"refreshed": refreshed}


# ======================== Notification Tasks ========================

@shared_task
def send_hitl_notification(user_id: int, request_id: str, channel: str = 'websocket'):
    """
    Send HITL notification to user.
    
    Args:
        user_id: User to notify
        request_id: HITL request ID
        channel: Notification channel (websocket, email, push)
    """
    import asyncio
    from streaming.consumers import send_hitl_request_to_user
    from orchestrator.models import HITLRequest
    
    try:
        request = HITLRequest.objects.get(request_id=request_id)
    except HITLRequest.DoesNotExist:
        return {"error": "Request not found"}
    
    request_data = {
        'request_id': str(request.request_id),
        'type': request.request_type,
        'title': request.title,
        'message': request.message,
        'options': request.options,
    }
    
    if channel == 'websocket':
        asyncio.run(send_hitl_request_to_user(user_id, request_data))
    elif channel == 'email':
        # TODO: Implement email notification
        pass
    elif channel == 'push':
        # TODO: Implement push notification
        pass
    
    return {"sent": True, "channel": channel}
