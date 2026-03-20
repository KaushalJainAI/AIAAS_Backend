"""
Orchestrator App API Views

Workflow CRUD, execution control, and HITL endpoints.
"""
import logging
from uuid import UUID

from django.shortcuts import get_object_or_404
from rest_framework import status
from adrf.decorators import api_view
from rest_framework.decorators import permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from asgiref.sync import sync_to_async

from .models import Workflow, WorkflowVersion, HITLRequest, ConversationMessage
from .serializers import (
    WorkflowSerializer, 
    WorkflowVersionSerializer, 
    HITLRequestSerializer, 
    ConversationMessageSerializer
)
from compiler.validators import (
    validate_dag,
    validate_credentials,
    validate_node_configs,
    validate_type_compatibility,
)
from logs.models import ExecutionLog
from executor.trigger_manager import get_trigger_manager
from executor.king import AuthorizationError, StateConflictError

logger = logging.getLogger(__name__)


def is_functionally_identical(nodes1, edges1, nodes2, edges2):
    """
    Compare two workflow definitions functionally.
    Ignores layout/aesthetic properties like 'position' or 'selected'.
    """
    def sanitize_nodes(nodes):
        sanitized = []
        for node in nodes:
            # Create a clean version of the node with only logic-affecting properties
            n = {
                'id': node.get('id'),
                'type': node.get('type'),
                'nodeType': node.get('nodeType') or node.get('data', {}).get('nodeType'),
                'data': {
                    'config': node.get('data', {}).get('config', {}),
                    'nodeType': node.get('data', {}).get('nodeType'),
                    # Exclude 'label', 'icon', 'color' if they don't affect logic? 
                    # Usually 'config' is the main logic part.
                }
            }
            sanitized.append(n)
        # Sort by ID for deterministic comparison
        return sorted(sanitized, key=lambda x: str(x.get('id', '')))

    def sanitize_edges(edges):
        sanitized = []
        for edge in edges:
            e = {
                'id': edge.get('id'),
                'source': edge.get('source'),
                'target': edge.get('target'),
                'sourceHandle': edge.get('sourceHandle'),
                'targetHandle': edge.get('targetHandle'),
            }
            sanitized.append(e)
        # Sort by source+target for deterministic comparison
        return sorted(sanitized, key=lambda x: f"{x.get('source')}-{x.get('target')}-{x.get('sourceHandle')}")

    return sanitize_nodes(nodes1) == sanitize_nodes(nodes2) and \
           sanitize_edges(edges1) == sanitize_edges(edges2)


# ======================== Workflow CRUD API ========================

@api_view(['GET', 'POST'])
@permission_classes([IsAuthenticated])
def workflow_list(request):
    """
    GET: List user's workflows
    POST: Create new workflow
    """
    if request.method == 'GET':
        status_filter = request.query_params.get('status')
        qs = Workflow.objects.filter(user=request.user)
        
        if status_filter:
            qs = qs.filter(status=status_filter)
        
        serializer = WorkflowSerializer(qs.order_by('-updated_at'), many=True)
        return Response(serializer.data)
    
    elif request.method == 'POST':
        # Use serializer for initial validation
        serializer = WorkflowSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        
        # Ensure unique name (manual logic preserved as requested by nature of app)
        base_name = serializer.validated_data.get('name', 'Untitled Workflow')
        name = base_name
        counter = 1
        while Workflow.objects.filter(user=request.user, name=name).exists():
            name = f"{base_name} ({counter})"
            counter += 1
        
        workflow = serializer.save(user=request.user, name=name)
        
        return Response(WorkflowSerializer(workflow).data, status=status.HTTP_201_CREATED)


@api_view(['GET', 'PUT', 'DELETE'])
@permission_classes([IsAuthenticated])
def workflow_detail(request, workflow_id: int):
    """
    GET: Get workflow details
    PUT: Update workflow
    DELETE: Delete workflow
    """
    workflow = get_object_or_404(Workflow, id=workflow_id, user=request.user)
    
    if request.method == 'GET':
        return Response(WorkflowSerializer(workflow).data)
    
    elif request.method == 'PUT':
        # Use serializer for update validation
        serializer = WorkflowSerializer(workflow, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        
        # VERSIONING: Create a snapshot of the current state before update
        last_version = WorkflowVersion.objects.filter(workflow=workflow).order_by('-version_number').first()
        next_version_num = (last_version.version_number + 1) if last_version else 1
        
        # [OMITTED VERSION LIMIT LOGIC FOR BREVITY - PRESERVED IN ACTUAL FILE]
        # (I will keep the version limit logic in the actual implementation)
        
        # VERSIONING: Create a snapshot of the current state before update
        last_version = WorkflowVersion.objects.filter(workflow=workflow).order_by('-version_number').first()
        next_version_num = (last_version.version_number + 1) if last_version else 1
        
        # Limit version history to 50 versions
        MAX_VERSIONS = 50
        current_versions = WorkflowVersion.objects.filter(workflow=workflow).order_by('version_number')
        if current_versions.count() >= MAX_VERSIONS:
            # Delete oldest versions to keep total at MAX_VERSIONS after new one
            to_delete_count = current_versions.count() - MAX_VERSIONS + 1
            if to_delete_count > 0:
                to_delete_ids = list(current_versions.values_list('id', flat=True)[:to_delete_count])
                WorkflowVersion.objects.filter(id__in=to_delete_ids).delete()

        WorkflowVersion.objects.create(
            workflow=workflow,
            version_number=next_version_num,
            label=f"Auto-save v{next_version_num}",
            nodes=workflow.nodes,
            edges=workflow.edges,
            workflow_settings=workflow.workflow_settings,
            created_by=request.user,
            change_summary="Auto-saved before modification",
        )

        # Perform custom logic for status change if present
        new_status = serializer.validated_data.get('status')
        if new_status == 'active' and workflow.status != 'active':
            # Run validations before allowing deployment
            temp_nodes = serializer.validated_data.get('nodes', workflow.nodes)
            temp_edges = serializer.validated_data.get('edges', workflow.edges)
            
            # 1. Static Validation
            errors = []
            errors.extend(validate_dag(temp_nodes, temp_edges))
            errors.extend(validate_node_configs(temp_nodes))
            
            from credentials.models import Credential
            user_credentials = set(map(str, Credential.objects.filter(user=request.user).values_list('id', flat=True)))
            errors.extend(validate_credentials(temp_nodes, user_credentials))
            errors.extend(validate_type_compatibility(temp_nodes, temp_edges))
            
            if errors:
                # Convert Pydantic error models to dicts for JSON response
                error_details = [
                    e.model_dump(by_alias=True) if hasattr(e, 'model_dump') else str(e) 
                    for e in errors
                ]
                return Response({
                    "error": "Validation failed",
                    "message": "Workflow settings or structure are invalid.",
                    "details": error_details
                }, status=status.HTTP_400_BAD_REQUEST)
            
            # 2. Strict Runtime Validation: Functional Proof of Success
            from logs.models import ExecutionLog
            successful_executions = ExecutionLog.objects.filter(
                workflow=workflow, 
                status='completed'
            ).order_by('-completed_at')
            
            proof_found = False
            for log in successful_executions:
                snapshot = log.workflow_snapshot
                if not snapshot:
                    continue
                    
                snap_nodes = snapshot.get('nodes', [])
                snap_edges = snapshot.get('edges', [])
                
                if is_functionally_identical(temp_nodes, temp_edges, snap_nodes, snap_edges):
                    proof_found = True
                    break
            
            if not proof_found:
                return Response({
                    "error": "Deployment rejected",
                    "message": "The current workflow configuration has not been successfully tested yet. Please run a successful test before deploying.",
                    "tip": "Moving nodes (layout changes) is allowed, but changing node settings or connections requires a new successful run."
                }, status=status.HTTP_400_BAD_REQUEST)

            # Trigger Lifecycle Management
            try:
                from executor.trigger_manager import get_trigger_manager
                mgr = get_trigger_manager()
                mgr.register_triggers(workflow)
            except Exception as e:
                # Log but don't fail the request if trigger registration fails
                logger.error(f"Failed to manage triggers for workflow {workflow.id}: {e}")
        elif new_status and new_status in ('draft', 'paused', 'archived') and workflow.status == 'active':
            try:
                from executor.trigger_manager import get_trigger_manager
                mgr = get_trigger_manager()
                mgr.unregister_triggers(workflow.id)
            except Exception as e:
                logger.error(f"Failed to unregister triggers for workflow {workflow.id}: {e}")

        # Save through serializer
        workflow = serializer.save()
        
        return Response({
            **WorkflowSerializer(workflow).data,
            'version_created': next_version_num
        })
    
    elif request.method == 'DELETE':
        workflow.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


@api_view(['POST'])
@permission_classes([IsAuthenticated])
async def deploy_workflow(request, workflow_id: int):
    """
    Validates and activates a workflow.
    Ensures the workflow structure is valid and has a successful test run.
    """
    workflow = await Workflow.objects.filter(id=workflow_id, user=request.user).afirst()
    if not workflow:
        return Response({'error': 'Workflow not found'}, status=status.HTTP_404_NOT_FOUND)
    
    # 1. Static Validation
    errors = []
    errors.extend(validate_dag(workflow.nodes, workflow.edges))
    errors.extend(validate_node_configs(workflow.nodes))
    
    from credentials.models import Credential
    user_credentials = set(map(str, await sync_to_async(list)(Credential.objects.filter(user=request.user).values_list('id', flat=True))))
    errors.extend(validate_credentials(workflow.nodes, user_credentials))
    errors.extend(validate_type_compatibility(workflow.nodes, workflow.edges))
    
    if errors:
        error_details = [
            e.model_dump(by_alias=True) if hasattr(e, 'model_dump') else str(e) 
            for e in errors
        ]
        return Response({
            "error": "Validation failed",
            "message": "Workflow settings or structure are invalid.",
            "details": error_details
        }, status=status.HTTP_400_BAD_REQUEST)
    
    # 2. Strict Runtime Validation: Check for Proof of Success
    successful_executions = await sync_to_async(list)(ExecutionLog.objects.filter(
        workflow=workflow, 
        status='completed'
    ).order_by('-id')[:1]) # Get last success
    
    proof_found = False
    for log in successful_executions:
        snapshot = log.workflow_snapshot
        if snapshot and is_functionally_identical(workflow.nodes, workflow.edges, snapshot.get('nodes', []), snapshot.get('edges', [])):
            proof_found = True
            break
            
    if not proof_found:
        return Response({
            "error": "Deployment rejected",
            "message": "The current workflow configuration has not been successfully tested yet. Please run a successful test before deploying.",
            "tip": "Moving nodes (layout changes) is allowed, but changing node settings or connections requires a new successful run."
        }, status=status.HTTP_400_BAD_REQUEST)

    workflow.status = 'active'
    await workflow.asave()
    
    # Register Triggers
    try:
        mgr = get_trigger_manager()
        await sync_to_async(mgr.register_triggers)(workflow)
    except Exception as e:
        logger.exception(f"Failed to register triggers for workflow {workflow.id}: {e}")
        return Response({'error': f'Deployment partially failed: {str(e)}'}, status=500)
    
    return Response({
        'status': 'active',
        'message': 'Workflow deployed successfully'
    })


@api_view(['POST'])
@permission_classes([IsAuthenticated])
async def undeploy_workflow(request, workflow_id: int):
    """
    Deactivates a workflow and unregisters its triggers.
    """
    workflow = await Workflow.objects.filter(id=workflow_id, user=request.user).afirst()
    if not workflow:
        return Response({'error': 'Workflow not found'}, status=status.HTTP_404_NOT_FOUND)
    
    workflow.status = 'draft' # Move back to draft
    await workflow.asave()
    
    # Unregister Triggers
    try:
        mgr = get_trigger_manager()
        await sync_to_async(mgr.unregister_triggers)(workflow.id)
    except Exception as e:
        logger.exception(f"Failed to unregister triggers for workflow {workflow.id}: {e}")
        return Response({'error': f'Undeployment cleanup failed: {str(e)}'}, status=500)
        
    return Response({
        'status': 'draft',
        'message': 'Workflow undeployed successfully'
    })


# ======================== Execution Control API ========================

@api_view(['POST'])
@permission_classes([IsAuthenticated])
async def execute_workflow(request, workflow_id: int):
    """
    Start executing a workflow.
    
    Request body:
        - input_data: Initial input data (optional)
        - async: Return immediately with execution_id (default: true)
    """
    from executor.king import get_orchestrator
    
    workflow = await Workflow.objects.filter(id=workflow_id, user=request.user).afirst()
    if not workflow:
        return Response({'error': 'Workflow not found'}, status=status.HTTP_404_NOT_FOUND)
    
    input_data = request.data.get('input_data', {})
    is_async = request.data.get('async', True)
    
    # Get orchestrator
    orchestrator = get_orchestrator(request.user.id)
    
    # Apply user's LLM settings for orchestrator thought generation
    llm_provider = request.data.get('llm_provider')
    llm_model = request.data.get('llm_model')
    llm_credential = request.data.get('llm_credential')
    if llm_provider or llm_model or llm_credential:
        await orchestrator.update_settings(llm_type=llm_provider, llm_model=llm_model, credential_id=llm_credential)
    
    # Auto-inject user credentials
    from executor.credential_utils import get_workflow_credentials
    # Build workflow JSON
    workflow_json = {
        'id': workflow.id,
        'nodes': workflow.nodes,
        'edges': workflow.edges,
        'settings': workflow.workflow_settings,
    }
    
    # Decrypt ONLY required credentials for this workflow
    from asgiref.sync import sync_to_async
    active_creds = await sync_to_async(get_workflow_credentials)(request.user.id, workflow_json)
    
    # Start Execution via Orchestrator
    try:
        handle = await orchestrator.start(
            workflow_json=workflow_json,
            user_id=request.user.id,
            input_data=input_data,
            credentials=active_creds, # Pass injected creds
            supervision=workflow.supervision_level, 
            context=workflow.context,
        )
    except Exception as e:
        logger.error(f"Failed to start workflow: {e}")
        return Response({
            'error': 'Orchestrator Failure',
            'message': str(e)
        }, status=status.HTTP_400_BAD_REQUEST)
    
    return Response({
        'execution_id': str(handle.execution_id),
        'workflow_id': workflow.id,
        'state': handle.state.value,
        'started_at': handle.started_at,
    })
    


@api_view(['POST'])
@permission_classes([IsAuthenticated])
async def update_orchestrator_settings(request):
    """
    Update the orchestrator's LLM settings (provider & model).
    Called from the Orchestrator page when user changes the model config.
    """
    from executor.king import get_orchestrator
    
    llm_provider = request.data.get('llm_provider')
    llm_model = request.data.get('llm_model')
    llm_credential = request.data.get('llm_credential')
    
    if not llm_provider and not llm_model and not llm_credential:
        return Response({'error': 'No settings provided'}, status=status.HTTP_400_BAD_REQUEST)
    
    orchestrator = get_orchestrator(request.user.id)
    await orchestrator.update_settings(llm_type=llm_provider, llm_model=llm_model, credential_id=llm_credential)
    
    return Response({
        'status': 'ok',
        'llm_type': orchestrator.llm_type,
        'llm_model': orchestrator.llm_model,
        'credential_id': orchestrator.credential_id,
    })



@api_view(['POST'])
@permission_classes([IsAuthenticated])
async def pause_execution(request, execution_id: str):
    """Pause a running execution."""
    from executor.king import get_orchestrator
    
    orchestrator = get_orchestrator(request.user.id)
    
    try:
        exec_uuid = UUID(execution_id)
        result = await orchestrator.pause(exec_uuid)
    except ValueError:
        return Response({'error': 'Invalid execution ID format'}, status=status.HTTP_400_BAD_REQUEST)
    except AuthorizationError:
        return Response({'error': 'Execution not found or not authorized'}, status=status.HTTP_404_NOT_FOUND)
    except StateConflictError as e:
        return Response({'error': str(e)}, status=status.HTTP_409_CONFLICT)
    
    if result:
        return Response({'status': 'paused', 'execution_id': execution_id})
    return Response({'error': 'Could not pause execution due to an internal orchestrator state issue'}, status=500)


@api_view(['POST'])
@permission_classes([IsAuthenticated])
async def resume_execution(request, execution_id: str):
    """Resume a paused execution."""
    from executor.king import get_orchestrator
    
    orchestrator = get_orchestrator(request.user.id)
    
    try:
        exec_uuid = UUID(execution_id)
        result = await orchestrator.resume(exec_uuid)
    except ValueError:
        return Response({'error': 'Invalid execution ID format'}, status=status.HTTP_400_BAD_REQUEST)
    except AuthorizationError:
        return Response({'error': 'Execution not found or not authorized'}, status=status.HTTP_404_NOT_FOUND)
    except StateConflictError as e:
        return Response({'error': str(e)}, status=status.HTTP_409_CONFLICT)
    
    if result:
        return Response({'status': 'resumed', 'execution_id': execution_id})
    return Response({'error': 'Could not resume execution due to an internal orchestrator state issue'}, status=500)


@api_view(['POST'])
@permission_classes([IsAuthenticated])
async def stop_execution(request, execution_id: str):
    """Stop/cancel an execution."""
    from executor.king import get_orchestrator
    
    orchestrator = get_orchestrator(request.user.id)
    
    try:
        exec_uuid = UUID(execution_id)
        result = await orchestrator.stop(exec_uuid)
    except ValueError:
        return Response({'error': 'Invalid execution ID format'}, status=status.HTTP_400_BAD_REQUEST)
    except AuthorizationError:
        return Response({'error': 'Execution not found or not authorized'}, status=status.HTTP_404_NOT_FOUND)
    except StateConflictError as e:
        # Stop is usually idempotent, but if we raise a conflict, return it
        return Response({'error': str(e)}, status=status.HTTP_409_CONFLICT)
    
    if result:
        return Response({'status': 'stopped', 'execution_id': execution_id})
    return Response({'error': 'Could not stop execution'}, status=500)


@api_view(['GET'])
@permission_classes([IsAuthenticated])
async def execution_status(request, execution_id: str):
    """Get current execution status."""
    from executor.king import get_orchestrator
    
    orchestrator = get_orchestrator(request.user.id)
    
    try:
        exec_uuid = UUID(execution_id)
        handle = await orchestrator.get_status(exec_uuid)
    except ValueError:
        return Response({'error': 'Invalid execution ID format'}, status=status.HTTP_400_BAD_REQUEST)
    
    if not handle:
        return Response({'error': 'Execution not found or not authorized'}, status=404)
    
    return Response({
        'execution_id': str(handle.execution_id),
        'workflow_id': handle.workflow_id,
        'state': handle.state.value,
        'current_node': handle.current_node,
        'progress': handle.progress,
        'error': handle.error,
        'started_at': handle.started_at,
        'completed_at': handle.completed_at,
        'pending_hitl': {
            'id': handle.pending_hitl.id,
            'type': handle.pending_hitl.request_type.value,
            'message': handle.pending_hitl.message,
            'options': handle.pending_hitl.options,
        } if handle.pending_hitl else None,
    })


# ======================== HITL API ========================

@api_view(['GET'])
@permission_classes([IsAuthenticated])
def pending_hitl_requests(request):
    """Get all pending HITL requests for the user."""
    requests = HITLRequest.objects.filter(
        user=request.user,
        status='pending'
    ).order_by('-created_at')
    
    serializer = HITLRequestSerializer(requests, many=True)
    return Response({'pending': serializer.data, 'count': len(serializer.data)})


@api_view(['POST'])
@permission_classes([IsAuthenticated])
async def respond_to_hitl(request, request_id: str):
    """
    Respond to a HITL request.
    
    Request body:
        - action: 'approve', 'reject', 'answer', 'skip', 'retry'
        - value: Response value (for clarification)
        - message: Optional message
    """
    from executor.king import get_orchestrator
    
    try:
        hitl_request = await HITLRequest.objects.filter(
            request_id=request_id,
            user=request.user,
            status='pending'
        ).afirst()
        if not hitl_request:
            raise HITLRequest.DoesNotExist()
    except HITLRequest.DoesNotExist:
        return Response({'error': 'Request not found or already responded'}, status=404)
    
    action = request.data.get('action', 'approve')
    value = request.data.get('value')
    message = request.data.get('message', '')
    
    # Update HITL request
    from django.utils import timezone
    
    if action in ('approve', 'approved'):
        hitl_request.status = 'approved'
    elif action in ('reject', 'rejected'):
        hitl_request.status = 'rejected'
    else:
        hitl_request.status = 'answered'
    
    hitl_request.response = {
        'action': action,
        'value': value,
        'message': message,
    }
    hitl_request.responded_at = timezone.now()
    await hitl_request.asave()
    
    # Notify orchestrator
    orchestrator = get_orchestrator(request.user.id)
    await orchestrator.respond_to_hitl(
        request_id=request_id,
        response={'action': action, 'value': value},
    )
    
    return Response({
        'request_id': request_id,
        'status': hitl_request.status,
        'responded_at': hitl_request.responded_at,
    })


# ======================== AI Chat API ========================

@api_view(['GET', 'POST', 'DELETE'])
@permission_classes([IsAuthenticated])
def conversation_messages(request, conversation_id: str = None, message_id: int = None):
    """
    GET: Get conversation history
    POST: Add a message (and get AI response)
    DELETE: Delete a conversation (or a message if message_id provided)
    """
    from uuid import uuid4
    
    from django.db.models import Max

    if request.method == 'GET':
        if not conversation_id:
            # List recent conversations
            conversations = (
                ConversationMessage.objects
                .filter(user=request.user)
                .values('conversation_id')
                .annotate(last_active=Max('created_at'))
                .order_by('-last_active')[:20]
            )
            return Response({'conversations': list(conversations)})
        
        messages = ConversationMessage.objects.filter(
            user=request.user,
            conversation_id=conversation_id
        ).order_by('created_at')
        
        return Response({'messages': ConversationMessageSerializer(messages, many=True).data})
    
    elif request.method == 'POST':
        content = request.data.get('content', '')
        workflow_id = request.data.get('workflow_id')
        conv_id = conversation_id or str(uuid4())
        
        # Save user message
        user_msg = ConversationMessage.objects.create(
            user=request.user,
            conversation_id=conv_id,
            workflow_id=workflow_id,
            role='user',
            content=content,
        )
        
        # TODO: Call AI for response (integrate with LLM nodes)
        # For now, return a placeholder
        ai_response = "I understand you're asking about your workflow. This feature is coming soon!"
        
        # Save AI response
        ai_msg = ConversationMessage.objects.create(
            user=request.user,
            conversation_id=conv_id,
            workflow_id=workflow_id,
            role='assistant',
            content=ai_response,
            metadata={'model': 'placeholder'},
        )
        
        return Response({
            'conversation_id': conv_id,
            'user_message': {'id': user_msg.id, 'content': content, 'created_at': user_msg.created_at},
            'ai_response': {'id': ai_msg.id, 'content': ai_response, 'created_at': ai_msg.created_at},
        })
    
    elif request.method == 'DELETE':
        if not conversation_id:
            return Response({'error': 'Conversation ID required'}, status=400)
            
        if message_id is not None:
            is_rewind = request.query_params.get('rewind', '').lower() == 'true'
            is_rewind_after = request.query_params.get('rewind_after', '').lower() == 'true'
            
            if is_rewind_after:
                deleted_count, _ = ConversationMessage.objects.filter(
                    user=request.user,
                    conversation_id=conversation_id,
                    id__gt=message_id
                ).delete()
            elif is_rewind:
                deleted_count, _ = ConversationMessage.objects.filter(
                    user=request.user,
                    conversation_id=conversation_id,
                    id__gte=message_id
                ).delete()
            else:
                deleted_count, _ = ConversationMessage.objects.filter(
                    user=request.user,
                    conversation_id=conversation_id,
                    id=message_id
                ).delete()
        else:
            deleted_count, _ = ConversationMessage.objects.filter(
                user=request.user,
                conversation_id=conversation_id
            ).delete()
        
        return Response({'deleted': True, 'count': deleted_count})

    return Response({'error': f'Method {request.method} not allowed'}, status=405)


# ======================== Version History API ========================

@api_view(['GET', 'POST'])
@permission_classes([IsAuthenticated])
def workflow_versions(request, workflow_id: int):
    """
    GET: List versions
    POST: Create new version (snapshot)
    """
    workflow = get_object_or_404(Workflow, id=workflow_id, user=request.user)
    
    if request.method == 'GET':
        versions = WorkflowVersion.objects.filter(workflow=workflow).order_by('-version_number')
        return Response({'versions': WorkflowVersionSerializer(versions, many=True).data})
    
        # Use serializer for validation
        serializer = WorkflowVersionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        
        # Get next version number
        last_version = WorkflowVersion.objects.filter(workflow=workflow).order_by('-version_number').first()
        next_version = (last_version.version_number + 1) if last_version else 1
        
        # Limit version history logic (redundant but kept for specific manual triggers)
        MAX_VERSIONS = 10
        current_versions = WorkflowVersion.objects.filter(workflow=workflow).order_by('version_number')
        if current_versions.count() >= MAX_VERSIONS:
            to_delete_count = current_versions.count() - MAX_VERSIONS + 1
            if to_delete_count > 0:
                to_delete_ids = list(current_versions.values_list('id', flat=True)[:to_delete_count])
                WorkflowVersion.objects.filter(id__in=to_delete_ids).delete()

        version = serializer.save(
            workflow=workflow,
            version_number=next_version,
            nodes=workflow.nodes,
            edges=workflow.edges,
            workflow_settings=workflow.workflow_settings,
            created_by=request.user
        )
        
        return Response(WorkflowVersionSerializer(version).data, status=status.HTTP_201_CREATED)


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def restore_version(request, workflow_id: int, version_id: int):
    """Restore workflow to a specific version."""
    workflow = get_object_or_404(Workflow, id=workflow_id, user=request.user)
    version = get_object_or_404(WorkflowVersion, id=version_id, workflow=workflow)
    
    # Restore from version
    workflow.nodes = version.nodes
    workflow.edges = version.edges
    workflow.workflow_settings = version.workflow_settings
    workflow.save()
    
    return Response({
        'restored': True,
        'version_number': version.version_number,
        'workflow_id': workflow.id,
    })


# ======================== AI Workflow Generation API ========================

@api_view(['POST'])
@permission_classes([IsAuthenticated])
async def generate_workflow(request):
    """
    Generate a workflow from natural language description.
    
    Request body:
        - description: What the workflow should do
        - credential_id: LLM credential ID (optional)
    """
    description = request.data.get('description', '')
    credential_id = request.data.get('credential_id')
    conversation_id = request.data.get('conversation_id')
    provider = request.data.get('provider')
    model = request.data.get('model')
    
    if not description:
        return Response({'error': 'Description is required'}, status=400)

    from executor.king import get_orchestrator
    orchestrator = get_orchestrator(request.user.id)
    
    # Update settings if provided
    if provider or model:
        orchestrator.update_settings(llm_type=provider, llm_model=model)
    
    result = await orchestrator.create_workflow_from_intent(
        prompt=description,
        credential_id=credential_id,
    )
    
    if 'error' in result:
        return Response(result, status=400)
    
    # Optionally save the generated workflow
    if request.data.get('save', False):
        base_name = result.get('name', 'Generated Workflow')
        name = base_name
        counter = 1
        while await Workflow.objects.filter(user=request.user, name=name).aexists():
            name = f"{base_name} ({counter})"
            counter += 1
            
        workflow = await Workflow.objects.acreate(
            user=request.user,
            name=name,
            description=result.get('description', description),
            nodes=result.get('nodes', []),
            edges=result.get('edges', []),
            status='draft',
        )
        result['saved'] = True
        result['workflow_id'] = workflow.id

    # Save to conversation history
    from uuid import uuid4
    conv_id = conversation_id or str(uuid4())
    
    # User message
    await ConversationMessage.objects.acreate(
        user=request.user,
        conversation_id=conv_id,
        role='user',
        content=f"Generate workflow: {description}",
        metadata={'type': 'builder_generation'}
    )
    
    # AI response
    ai_content = f"I've created a workflow for you!\n\n**{result.get('name')}**\n\n{result.get('description')}"
    if result.get('saved'):
        ai_content += f"\n\nWorkflow ID: {result.get('workflow_id')}"
        
    await ConversationMessage.objects.acreate(
        user=request.user,
        conversation_id=conv_id,
        role='assistant',
        content=ai_content,
        metadata={
            'type': 'builder_generation',
            'workflow_id': result.get('workflow_id'),
            'nodes_count': len(result.get('nodes', [])),
        }
    )
    
    result['conversation_id'] = conv_id
    
    return Response(result)


@api_view(['POST'])
@permission_classes([IsAuthenticated])
async def modify_workflow(request, workflow_id: int):
    """
    Modify a workflow using natural language.
    
    Request body:
        - modification: What to change
        - credential_id: LLM credential ID (optional)
    """
    
    workflow = await Workflow.objects.filter(id=workflow_id, user=request.user).afirst()
    if not workflow:
        return Response({'error': 'Workflow not found'}, status=status.HTTP_404_NOT_FOUND)
        
    modification = request.data.get('modification', '')
    credential_id = request.data.get('credential_id')
    conversation_id = request.data.get('conversation_id')
    provider = request.data.get('provider')
    model = request.data.get('model')
    
    if not modification:
        return Response({'error': 'Modification description is required'}, status=400)

    from executor.king import get_orchestrator
    orchestrator = get_orchestrator(request.user.id)
    
    # Update settings if provided
    if provider or model:
        orchestrator.update_settings(llm_type=provider, llm_model=model)
    
    result = await orchestrator.modify_workflow(
        workflow=current_workflow,
        modification=modification,
        credential_id=credential_id,
    )
    
    if 'error' in result:
        return Response(result, status=400)
    
    # Apply changes if requested
    if request.data.get('apply', False):
        new_name = result.get('name', workflow.name)
        if new_name != workflow.name:
            # Ensure uniqueness if name changed
            base_name = new_name
            name = base_name
            counter = 1
            while await Workflow.objects.filter(user=request.user, name=name).exclude(id=workflow.id).aexists():
                name = f"{base_name} ({counter})"
                counter += 1
            workflow.name = name
            
        workflow.description = result.get('description', workflow.description)
        workflow.nodes = result.get('nodes', workflow.nodes)
        workflow.edges = result.get('edges', workflow.edges)
        await workflow.asave()
        result['applied'] = True

    # Save to conversation history
    from uuid import uuid4
    conv_id = conversation_id or str(uuid4())
    
    # User message
    await ConversationMessage.objects.acreate(
        user=request.user,
        conversation_id=conv_id,
        workflow=workflow,
        role='user',
        content=f"Modify workflow: {modification}",
        metadata={'type': 'builder_modification'}
    )
    
    # AI response
    ai_content = f"I've modified the workflow based on your request.\n\nChanges:\n{result.get('explanation', 'Workflow updated successfully.')}"
    
    await ConversationMessage.objects.acreate(
        user=request.user,
        conversation_id=conv_id,
        workflow=workflow,
        role='assistant',
        content=ai_content,
        metadata={
            'type': 'builder_modification',
            'changes': result.get('changes', []),
        }
    )
    
    result['conversation_id'] = conv_id
    
    return Response(result)


@api_view(['GET'])
@permission_classes([IsAuthenticated])
async def suggest_improvements(request, workflow_id: int):
    """Get AI suggestions for workflow improvements."""
    from executor.king import get_orchestrator
    
    workflow = await Workflow.objects.filter(id=workflow_id, user=request.user).afirst()
    if not workflow:
        return Response({'error': 'Workflow not found'}, status=status.HTTP_404_NOT_FOUND)
    
    credential_id = request.query_params.get('credential_id')
    
    current_workflow = {
        'name': workflow.name,
        'nodes': workflow.nodes,
        'edges': workflow.edges,
    }
    
    orchestrator = get_orchestrator(request.user.id)
    
    import json
    prompt = f"""Analyze this workflow and suggest improvements:
{json.dumps(current_workflow, indent=2)}

Provide suggestions as a JSON array of objects with 'title', 'description', and 'priority' (high/medium/low)."""
    
    try:
        response = await orchestrator._call_llm(prompt, credential_id=credential_id)
        suggestions = orchestrator._parse_json_response(response)
    except Exception:
        suggestions = []
    
    return Response({
        'workflow_id': workflow_id,
        'suggestions': suggestions,
    })


# ======================== Context-Aware Chat API ========================

@api_view(['POST'])
@permission_classes([IsAuthenticated])
async def context_aware_chat(request):
    """
    Send a context-aware chat message.
    
    Request body:
        - message: User's message
        - workflow_id: Optional workflow context
        - node_id: Optional focused node
        - conversation_id: Optional conversation ID
        - credential_id: LLM credential ID
    """
    from .chat_context import ContextAwareChat
    
    message = request.data.get('message', '')
    workflow_id = request.data.get('workflow_id')
    node_id = request.data.get('node_id')
    conversation_id = request.data.get('conversation_id')
    credential_id = request.data.get('credential_id')
    provider = request.data.get('provider')
    
    if not message:
        return Response({'error': 'Message is required'}, status=400)
    
    chat = ContextAwareChat(user_id=request.user.id, llm_type=provider or 'openrouter')
    
    result = await chat.send_message(
        message=message,
        workflow_id=workflow_id,
        node_id=node_id,
        conversation_id=conversation_id,
        credential_id=credential_id,
    )
    
    return Response(result)


# ======================== Partial Execution API ========================

@api_view(['POST'])
@permission_classes([IsAuthenticated])
async def execute_partial(request, workflow_id: int = None):
    """
    Execute a single node with provided input and config.
    Used for "Test Step" functionality in the UI.
    
    Request body:
        - node_id: ID of the node
        - node_type: Type of the node
        - input_data: Input data for the node
        - config: Node configuration
    """
    from uuid import uuid4
    from nodes.handlers.registry import get_registry
    from compiler.schemas import ExecutionContext
    
    import logging
    logger = logging.getLogger(__name__)

    logger.debug(f"Partial execution request: {request.data}")
    try:
        node_id = request.data.get('node_id')
        node_type = request.data.get('node_type')
        input_data = request.data.get('input_data', {})
        config = request.data.get('config', {})
        
        # --- UI Helper: Smart context and input data fetching ---
        node_outputs = {}
        node_label_to_id = {}
        
        target_workflow_id = workflow_id or request.data.get('workflow_id')
        if target_workflow_id:
            try:
                from logs.models import ExecutionLog, NodeExecutionLog
                from orchestrator.models import Workflow
                
                workflow = await Workflow.objects.filter(id=target_workflow_id).afirst()
                if workflow:
                    # 1. Build label to ID map for expression resolution
                    for node in workflow.nodes:
                        label = node.get('data', {}).get('label')
                        if label:
                            node_label_to_id[label] = node.get('id')
                    
                    # 2. Try to fetch data from the last successful run to populate context
                    last_exec = await ExecutionLog.objects.filter(
                        workflow_id=target_workflow_id,
                        status='completed'
                    ).order_by('-created_at').afirst()
                    
                    if last_exec:
                        # Load all node outputs from that execution into context
                        # This allows expressions like {{ $node["Name"] }} to resolve
                        async for n_log in NodeExecutionLog.objects.filter(execution=last_exec):
                            node_outputs[n_log.node_id] = n_log.output_data
                        
                        logger.info(f"Populated {len(node_outputs)} node outputs from execution {last_exec.execution_id} for partial test")

                        # 3. Auto-fill input_data if empty by looking at predecessors
                        if not input_data:
                            edges = workflow.edges
                            preceding_node_ids = [
                                edge['source'] for edge in edges 
                                if edge.get('target') == node_id
                            ]
                            
                            if preceding_node_ids:
                                # Find the output of the most recent preceding node in that execution
                                node_log = await NodeExecutionLog.objects.filter(
                                    execution=last_exec,
                                    node_id__in=preceding_node_ids
                                ).order_by('-execution_order').afirst()
                                
                                if node_log and node_log.output_data:
                                    # AIAAS nodes usually return {'items': [{'json': {...}}]}
                                    raw_output = node_log.output_data
                                    if isinstance(raw_output, dict) and 'items' in raw_output and raw_output['items']:
                                        input_data = raw_output['items'][0].get('json', {})
                                    else:
                                        input_data = raw_output
                                    logger.info(f"Auto-filled input_data for node {node_id} from predecessor {node_log.node_id}")
            except Exception as fe:
                logger.warning(f"Failed to populate partial execution context: {fe}")
        
        # 4. Manual test data from node config (overrides auto-fill if present)
        manual_test_data = config.get('test_data')
        if manual_test_data and isinstance(manual_test_data, dict):
             # Only override if input_data wasn't explicitly provided in the request body
             # (but keep it if we just auto-filled it and manual test data is a specific "mock")
             input_data = manual_test_data
             logger.info(f"Using manual test_data for node {node_id}")
        
        # Also try to get workflow_id from request data if not in URL
        if not workflow_id:
            raw_workflow_id = request.data.get('workflow_id')
            if raw_workflow_id:
                try:
                    workflow_id = int(raw_workflow_id)
                except (ValueError, TypeError):
                    pass
        
        if not node_id or not node_type:
            logger.error(f"Partial execution missing required fields: node_id={node_id}, node_type={node_type}")
            return Response({'detail': 'node_id and node_type are required'}, status=400)
        
        registry = get_registry()
        
        if not registry.has_handler(node_type):
            logger.error(f"Partial execution unknown node type: {node_type}")
            return Response({'detail': f'Unknown node type: {node_type}'}, status=400)
        
        handler = registry.get_handler(node_type)
        
        # --- Fetch credentials referenced in the node config ---
        credentials = {}
        # Support multiple field names for the credential ID
        credential_id = config.get('credential') or config.get('credential_id') or config.get('credentialId')
        
        if credential_id:
            from credentials.manager import CredentialManager
            cred_manager = CredentialManager()
            try:
                cred_data = await cred_manager.get_credential(
                    credential_id=credential_id,
                    user_id=request.user.id
                )
                if cred_data:
                    # Store under both raw and string key to handle int/string mismatches
                    credentials[credential_id] = cred_data
                    credentials[str(credential_id)] = cred_data
                else:
                    logger.warning(f"Credential {credential_id} not found for user {request.user.id}")
            except Exception as e:
                logger.error(f"Failed to fetch credential {credential_id}: {e}")
        
        # Create context with credentials and previous outputs for expression resolution
        context = ExecutionContext(
            execution_id=uuid4(),
            user_id=request.user.id,
            workflow_id=workflow_id or 0,
            node_id=node_id,
            credentials=credentials,
            node_outputs=node_outputs,
            node_label_to_id=node_label_to_id,
        )
        
        # Resolve expressions in config using the populated context
        resolved_config = context.resolve_all_expressions(config)
        
        result = await handler.execute(input_data, resolved_config, context)
        
        if result.success:
            # Return items array in n8n-compatible format
            items = [item.model_dump() for item in result.items]
            return Response({'items': items})
        else:
            error_msg = result.error or "Unknown node execution error"
            logger.warning(f"Partial execution handler returned error: {error_msg}")
            return Response({'detail': error_msg}, status=400)
            
    except Exception as e:
        import traceback
        logger.error(f"Partial execution failed with exception: {e}\n{traceback.format_exc()}")
        return Response({'detail': str(e)}, status=500)


# ======================== Thought History API ========================

@api_view(['GET'])
@permission_classes([IsAuthenticated])
def thought_history(request, execution_id: str):
    """Get thought history for an execution."""
    from .chat_context import get_thought_history
    
    history = get_thought_history(execution_id)
    
    return Response({
        'execution_id': execution_id,
        'thoughts': history.get_thoughts(),
        'summary': history.to_summary(),
    })


# ======================== Template & Testing API ========================

@api_view(['POST'])
@permission_classes([IsAuthenticated])
def clone_workflow(request, workflow_id: int):
    """Clone an existing workflow."""
    original = get_object_or_404(Workflow, id=workflow_id, user=request.user)
    
    base_name = f"{original.name} (Clone)"
    name = base_name
    counter = 1
    while Workflow.objects.filter(user=request.user, name=name).exists():
        name = f"{base_name} ({counter})"
        counter += 1
    
    clone = Workflow.objects.create(
        user=request.user,
        name=name,
        description=original.description,
        nodes=original.nodes,
        edges=original.edges,
        workflow_settings=original.workflow_settings,
        status='draft',
        icon=original.icon,
        color=original.color,
        tags=original.tags
    )
    
    return Response({
        'id': clone.id,
        'name': clone.name,
        'status': clone.status
    }, status=status.HTTP_201_CREATED)


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def test_workflow(request, workflow_id: int):
    """Run a test execution."""
    from executor.tasks import test_workflow_async
    
    workflow = get_object_or_404(Workflow, id=workflow_id, user=request.user)
    
    task = test_workflow_async.delay(workflow.id)
    
    return Response({
        'task_id': task.id,
        'status': 'queued',
        'workflow_id': workflow.id
    })


# ============================================================
# Export Views (merged from export_views.py)
# ============================================================

import io
import zipfile
import tempfile
from pathlib import Path
from django.core.management import call_command


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def export_workflow_zip(request, workflow_id):
    """
    Export a workflow as a standalone Flask app ZIP file.
    
    POST /api/workflows/{workflow_id}/export/
    → Returns: application/zip
    """
    from orchestrator.models import Workflow
    from django.http import HttpResponse

    # Verify ownership
    try:
        workflow = Workflow.objects.get(id=workflow_id, user=request.user)
    except Workflow.DoesNotExist:
        return HttpResponse(
            '{"error": "Workflow not found"}',
            content_type='application/json',
            status=404
        )

    # Generate into a temp directory
    with tempfile.TemporaryDirectory() as tmpdir:
        output_dir = Path(tmpdir) / 'export'
        output_dir.mkdir()

        # Call the management command programmatically
        try:
            call_command(
                'export_standalone',
                workflow_id,
                output_dir=str(output_dir),
                zip=False,  # We'll ZIP it ourselves for the response
            )
        except Exception as e:
            return HttpResponse(
                f'{{"error": "Export failed: {str(e)}"}}',
                content_type='application/json',
                status=500
            )

        # Find the generated folder (named after the workflow)
        export_folders = [d for d in output_dir.iterdir() if d.is_dir()]
        if not export_folders:
            return HttpResponse(
                '{"error": "Export produced no output"}',
                content_type='application/json',
                status=500
            )

        export_folder = export_folders[0]
        safe_name = export_folder.name

        # Create ZIP in memory
        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zf:
            for file in export_folder.rglob('*'):
                if file.is_file():
                    arcname = str(Path(safe_name) / file.relative_to(export_folder))
                    zf.write(file, arcname)

        zip_buffer.seek(0)

        response = HttpResponse(zip_buffer.read(), content_type='application/zip')
        response['Content-Disposition'] = f'attachment; filename="{safe_name}.zip"'
        return response


# ============================================================
# Webhook Views (merged from webhook_views.py)
# ============================================================

import json as _json
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from executor.trigger_manager import get_trigger_manager
from executor.tasks import execute_workflow_async

import logging as _logging
_webhook_logger = _logging.getLogger(__name__)


@csrf_exempt
def receive_webhook(request, user_id, webhook_path):
    """
    Public entry point for external webhooks.
    Matches user_id and path in Redis registry.
    """
    mgr = get_trigger_manager()
    config = mgr.lookup_webhook(user_id, webhook_path)
    
    if not config:
        _webhook_logger.warning(f"Webhook not found: {user_id}/{webhook_path}")
        return JsonResponse({"error": "Webhook not found"}, status=404)

    # 1. Validate Method
    allowed_method = config.get("method", "POST").upper()
    if request.method != allowed_method:
        return JsonResponse({"error": f"Method {request.method} not allowed. Use {allowed_method}"}, status=405)

    # 2. Validate Authentication
    auth_type = config.get("authentication", "none")
    node_type = config.get("node_type", "webhook_trigger")
    
    # Telegram Security Check
    if node_type == "telegram_trigger":
        secret_token = config.get("secret_token")
        if secret_token:
            incoming_token = request.headers.get('X-Telegram-Bot-Api-Secret-Token')
            if incoming_token != secret_token:
                _webhook_logger.warning(f"Telegram webhook rejected: Invalid secret token for {user_id}/{webhook_path}")
                return JsonResponse({"error": "Unauthorized - Invalid Secret Token"}, status=401)

    # Generic Webhook Auth Check
    if auth_type != "none":
        auth_key = config.get("auth_key", "")
        if auth_type == "header":
            if auth_key not in request.headers:
                return JsonResponse({"error": "Unauthorized - Missing Header"}, status=401)
        elif auth_type == "query":
            if auth_key not in request.GET:
                return JsonResponse({"error": "Unauthorized - Missing Query Parameter"}, status=401)

    # 3. Parse Body
    body_data = {}
    if request.body:
        try:
            body_data = _json.loads(request.body)
        except _json.JSONDecodeError:
            # If not JSON, try POST form data
            if request.POST:
                body_data = dict(request.POST)
            else:
                # Raw body if it's text or something else
                body_data = {"raw": request.body.decode('utf-8', errors='ignore')}

    # 4. Build input_data for execution
    github_event = request.headers.get('X-GitHub-Event')
    node_type = config.get("node_type")
    is_github = github_event or node_type == "github_trigger"
    is_telegram = node_type == "telegram_trigger"
    
    input_data = {
        "headers": dict(request.headers),
        "body": body_data,
        "query": dict(request.GET),
        "method": request.method,
        "url": request.build_absolute_uri(),
    }

    if is_github:
        input_data.update({
            "trigger_type": "github",
            "event": github_event or "push",
            "action": body_data.get("action", ""),
            "payload": body_data,
            "sender": body_data.get("sender", {}),
            "ref": body_data.get("ref", ""),
        })
    elif is_telegram:
        input_data.update({
            "trigger_type": "telegram",
            "payload": body_data,
        })
    else:
        input_data["trigger_type"] = "webhook"


    # 5. Dispatch Execution
    workflow_id = config.get("workflow_id")
    target_user_id = config.get("user_id")
    
    _webhook_logger.info(f"Triggering workflow {workflow_id} via webhook {user_id}/{webhook_path}")
    
    # We use .delay() to send it to Celery
    task = execute_workflow_async.delay(
        workflow_id=workflow_id,
        user_id=target_user_id,
        input_data=input_data
    )

    return JsonResponse({
        "status": "accepted",
        "execution_id": task.id,
        "message": "Workflow execution queued"
    }, status=202)


@api_view(['GET'])
@permission_classes([IsAuthenticated])
async def background_tasks(request):
    """
    Get active background tasks for the user.
    
    Includes:
    - Running workflow executions
    - Document processing/indexing tasks (if possible to track)
    """
    from logs.models import ExecutionLog
    from django.utils import timezone
    from datetime import timedelta
    
    # Get running executions from the last 24 hours
    since = timezone.now() - timedelta(hours=24)
    
    # On-demand Zombicide: Mark executions as failed if heartbeat is lost
    try:
        zombie_cutoff = timezone.now() - timedelta(minutes=5)
        # We REAP 'running' and 'pending' that haven't been touched in 5m
        
        @sync_to_async
        def find_and_reap_zombies(cutoff, user_id):
            zombies = ExecutionLog.objects.filter(
                user_id=user_id,
                status__in=['running', 'pending'],
                updated_at__lt=cutoff
            )
            zombie_ids = list(zombies.values_list('execution_id', flat=True))
            if zombie_ids:
                zombies.update(
                    status='failed',
                    error_message='Execution stalled (heartbeat loss detected during active task check)',
                    completed_at=timezone.now()
                )
            return zombie_ids

        zombie_ids = await find_and_reap_zombies(zombie_cutoff, request.user.id)
        
        if zombie_ids:
            logger.warning(f"On-demand Zombicide for user {request.user.id}: Reaped {len(zombie_ids)} zombies.")
            # Broadcast failures
            from streaming.broadcaster import get_broadcaster
            broadcaster = get_broadcaster()
            for eid in zombie_ids:
                asyncio.create_task(broadcaster.workflow_error(str(eid), "Stalled", ""))
    except Exception as e:
        logger.error(f"Error in on-demand Zombicide: {e}")

    active_executions = []
    async for log in ExecutionLog.objects.filter(
        user_id=request.user.id,
        status__in=['running', 'pending', 'waiting_human'],
        created_at__gt=since
    ).select_related('workflow').order_by('-created_at'):
        active_executions.append({
            'id': str(log.execution_id),
            'type': 'workflow_execution',
            'name': log.workflow.name if log.workflow else f"Execution {str(log.execution_id)[:8]}",
            'status': log.status,
            'started_at': log.created_at.isoformat(),
            'workflow_id': log.workflow_id,
            'supervision_level': log.workflow.supervision_level if log.workflow else 'none',
        })
    
    history_executions = []
    async for log in ExecutionLog.objects.filter(
        user_id=request.user.id,
        status__in=['completed', 'failed', 'cancelled']
    ).select_related('workflow').order_by('-created_at')[:10]:
        history_executions.append({
            'id': str(log.execution_id),
            'type': 'workflow_execution',
            'name': log.workflow.name if log.workflow else f"Execution {str(log.execution_id)[:8]}",
            'status': log.status,
            'started_at': log.created_at.isoformat(),
            'workflow_id': log.workflow_id,
            'supervision_level': log.workflow.supervision_level if log.workflow else 'none',
        })
    
    return Response({
        'tasks': active_executions,
        'history': history_executions,
        'count': len(active_executions)
    })
@api_view(['GET'])
@permission_classes([IsAuthenticated])
async def thought_history(request, execution_id):
    """
    Get AI thought history for a specific execution.
    """
    from .chat_context import get_thought_history
    
    history = get_thought_history(str(execution_id))
    thoughts = history.get_thoughts()
    
    return Response({
        'execution_id': str(execution_id),
        'thoughts': thoughts,
        'summary': history.to_summary()
    })
@api_view(['GET'])
@permission_classes([IsAuthenticated])
def system_info(request):
    """Get system-wide configuration and metadata."""
    from django.conf import settings
    return Response({
        'public_url': getattr(settings, 'PUBLIC_URL', 'http://localhost:8000'),
        'debug': settings.DEBUG,
        'version': '1.0.0',
    })
