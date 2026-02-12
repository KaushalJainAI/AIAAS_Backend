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
    from executor.king import get_orchestrator
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
        orchestrator = get_orchestrator(user_id)
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
def execute_scheduled_workflow(workflow_id: int, user_id: int):
    """
    Execute a single scheduled workflow. Called by Celery Beat.
    
    This is triggered automatically at the interval configured in 
    the ScheduleTriggerNode's config (cron or interval).
    """
    from django.utils import timezone
    logger.info(f"Executing scheduled workflow {workflow_id} for user {user_id}")
    
    # We reuse the existing execute_workflow_async logic by calling it directly 
    # as a task so it runs in the worker pool.
    execute_workflow_async.delay(
        workflow_id=workflow_id,
        user_id=user_id,
        input_data={
            "trigger_type": "schedule",
            "triggered_at": timezone.now().isoformat(),
        }
    )
    return {"status": "triggered", "workflow_id": workflow_id}


@shared_task(bind=True)
def poll_workflow_trigger(self, workflow_id: int, node_id: str):
    """
    Poll a specific trigger node for new items.
    Used for Email, RSS, Sheets, etc.
    """
    import asyncio
    import redis
    from django.conf import settings
    from orchestrator.models import Workflow, TriggerState
    from nodes.registry import get_registry
    from compiler.schemas import ExecutionContext
    from credentials.manager import get_credential_manager
    
    # 1. Acquire Distributed Lock (for horizontal scalability)
    r = redis.from_url(settings.CELERY_BROKER_URL)
    lock_key = f"lock:poll:{workflow_id}:{node_id}"
    # 5 minute lock, with non-blocking check
    lock = r.lock(lock_key, timeout=300, blocking_timeout=0)
    
    if not lock.acquire(blocking=False):
        logger.warning(f"Poll task for {workflow_id}:{node_id} already running elsewhere.")
        return {"status": "skipped", "reason": "lock_active"}

    try:
        # 2. Fetch Workflow and Node Config
        try:
            workflow = Workflow.objects.get(id=workflow_id)
        except Workflow.DoesNotExist:
            return {"error": "Workflow not found"}

        node = next((n for n in workflow.nodes if n['id'] == node_id), None)
        if not node:
            return {"error": f"Node {node_id} not found in workflow {workflow_id}"}
            
        node_type = node.get('data', {}).get('nodeType')
        config = node.get('data', {}).get('config', {})
        
        # 3. Load Handler
        registry = get_registry()
        handler = registry.get_handler(node_type)
        if not handler:
            return {"error": f"Handler for {node_type} not found"}

        # 4. Get Current State (Cursor)
        trigger_state_obj, _ = TriggerState.objects.get_or_create(
            workflow=workflow,
            node_id=node_id
        )
        
        # 5. Build Context (needed for credentials/poll)
        cred_manager = get_credential_manager()
        # In a real environment, we'd fetch all relevant credentials for this workflow
        # For poll, we usually only need the one specified in config
        credentials = {}
        target_cred_name = config.get("credential")
        if target_cred_name:
            creds = asyncio.run(cred_manager.get_workflow_credentials(workflow))
            credentials = {c['name']: c['data'] for c in creds}

        context = ExecutionContext(
            workflow_id=str(workflow_id),
            execution_id=f"poll_{workflow_id}_{node_id}",
            credentials=credentials
        )

        # 6. Execute Poll
        new_items, updated_state = asyncio.run(handler.poll(config, trigger_state_obj.state, context))

        # 7. Dispatch Executions
        if new_items:
            logger.info(f"Poll for {node_type} found {len(new_items)} new items")
            for item in new_items:
                execute_workflow_async.delay(
                    workflow_id=workflow_id,
                    user_id=workflow.user_id,
                    input_data=item # This will be passed to Node.execute as input_data
                )
            
            # 8. Save Updated State
            trigger_state_obj.state = updated_state
            trigger_state_obj.save()

        return {"status": "success", "new_items": len(new_items)}

    except Exception as e:
        logger.exception(f"Polling failed for {workflow_id}:{node_id}: {e}")
        return {"status": "error", "message": str(e)}
    finally:
        lock.release()


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


# ======================== Testing & Metrics ========================

@shared_task(bind=True)
def test_workflow_async(self, workflow_id: int):
    """
    Run async test for a workflow.
    """
    import asyncio
    from orchestrator.models import Workflow, WorkflowTestResult
    from executor.test_generator import generate_test_input, validate_test_result
    from executor.king import get_orchestrator
    from logs.models import ExecutionLog

    try:
        workflow = Workflow.objects.get(id=workflow_id)
    except Workflow.DoesNotExist:
        return {"error": "Workflow not found"}

    # Generate test data
    try:
        test_input = generate_test_input(workflow)
    except Exception as e:
        return {"error": f"Failed to generate test input: {e}"}

    async def run_test():
        orchestrator = get_orchestrator(workflow.user_id)
        
        # Start execution
        handle = await orchestrator.start(
            workflow_json={
                 "id": workflow.id,
                 "name": workflow.name,
                 "nodes": workflow.nodes,
                 "edges": workflow.edges,
                 "settings": workflow.workflow_settings
            },
            user_id=workflow.user_id,
            input_data=test_input,
            # Mock credentials would go here
        )
        
        # Wait for completion (60s max for test)
        for _ in range(60):
            if handle.completed_at:
                break
            await asyncio.sleep(1)
            
        return handle

    try:
        handle = asyncio.run(run_test())
    except Exception as e:
        return {"status": "error", "message": f"Test execution error: {e}"}
    
    # Determine status
    status = "failed"
    if handle.state.value == "completed":
        status = "passed"
    elif handle.state.value == "timeout": # If handled by state
        status = "timeout"
        
    # Get Output
    output_data = {}
    try:
        log = ExecutionLog.objects.get(execution_id=handle.execution_id)
        output_data = log.output_data
    except ExecutionLog.DoesNotExist:
        pass
        
    # Validation
    validation = validate_test_result(output_data)
    if not validation.passed:
        status = "failed"
        
    # Record Result
    WorkflowTestResult.objects.create(
        workflow=workflow,
        status=status,
        test_input=test_input,
        test_output=output_data,
        execution_time_ms=(handle.completed_at - handle.started_at).total_seconds() * 1000 if handle.completed_at else None,
        error_message=handle.error or validation.error or "",
        schema_valid=validation.passed
    )
    
    return {"status": status, "execution_id": str(handle.execution_id)}


@shared_task
def update_template_metrics(template_id: int, success: bool, duration_ms: int):
    """
    Update usage metrics for a template.
    """
    from orchestrator.models import WorkflowTemplate
    from django.db.models import F
    
    try:
        template = WorkflowTemplate.objects.get(id=template_id)
        
        # Update rolling average for duration
        if template.average_duration_ms:
            # Weighted average based on usage_count, but simpler to just do exponential moving average or 
            # simple update if usage_count is small.
            # Let's do simple update: new_avg = (old_avg * count + new) / (count + 1)
            count = template.usage_count
            total_duration = template.average_duration_ms * count
            new_avg = (total_duration + duration_ms) / (count + 1)
            template.average_duration_ms = int(new_avg)
        else:
            template.average_duration_ms = duration_ms
            
        # Update success rate
        # Success rate is stored as 0.0 to 100.0? Field says FloatField(default=0.0).
        # Assuming 0-100.
        current_rate = template.success_rate or 0.0
        count = template.usage_count
        
        # Calculate new successes count derived from rate
        current_successes = (current_rate / 100.0) * count
        new_successes = current_successes + (1 if success else 0)
        
        template.usage_count = count + 1
        template.success_rate = (new_successes / (count + 1)) * 100.0
        
        template.save()
        
    except WorkflowTemplate.DoesNotExist:
        pass

