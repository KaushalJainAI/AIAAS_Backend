"""
Orchestrator App API Views

Workflow CRUD, execution control, and HITL endpoints.
"""
from uuid import UUID

from django.shortcuts import get_object_or_404
from rest_framework import status
from adrf.decorators import api_view
from rest_framework.decorators import permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from asgiref.sync import sync_to_async

from .models import Workflow, WorkflowVersion, HITLRequest, ConversationMessage
from compiler.validators import (
    validate_dag,
    validate_credentials,
    validate_node_configs,
    validate_type_compatibility,
)
from logs.models import ExecutionLog
from executor.trigger_manager import get_trigger_manager


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
        
        workflows = list(qs.order_by('-updated_at').values(
            'id', 'name', 'slug', 'description', 'status',
            'icon', 'color', 'tags', 'execution_count',
            'last_executed_at', 'created_at', 'updated_at'
        ))
        
        # Add node count
        for w in workflows:
            wf = qs.get(id=w['id'])
            w['node_count'] = len(wf.nodes) if isinstance(wf.nodes, list) else 0
        
        return Response(workflows)
    
    elif request.method == 'POST':
        data = request.data
        
        # Ensure unique name
        base_name = data.get('name', 'Untitled Workflow')
        name = base_name
        counter = 1
        while Workflow.objects.filter(user=request.user, name=name).exists():
            name = f"{base_name} ({counter})"
            counter += 1
        
        workflow = Workflow.objects.create(
            user=request.user,
            name=name,
            description=data.get('description', ''),
            nodes=data.get('nodes', []),
            edges=data.get('edges', []),
            viewport=data.get('viewport', {}),
            workflow_settings=data.get('workflow_settings', {}),
            status=data.get('status', 'draft'),
            icon=data.get('icon', ''),
            color=data.get('color', '#6366f1'),
            tags=data.get('tags', []),
        )
        
        return Response({
            'id': workflow.id,
            'name': workflow.name,
            'slug': workflow.slug,
            'status': workflow.status,
            'created_at': workflow.created_at,
        }, status=status.HTTP_201_CREATED)


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
        return Response({
            'id': workflow.id,
            'name': workflow.name,
            'slug': workflow.slug,
            'description': workflow.description,
            'nodes': workflow.nodes,
            'edges': workflow.edges,
            'viewport': workflow.viewport,
            'workflow_settings': workflow.workflow_settings,
            'supervision_level': workflow.supervision_level,
            'llm_provider': workflow.llm_provider,
            'llm_model': workflow.llm_model,
            'llm_credential_id': workflow.llm_credential_id,
            'status': workflow.status,
            'icon': workflow.icon,
            'color': workflow.color,
            'tags': workflow.tags,
            'execution_count': workflow.execution_count,
            'last_executed_at': workflow.last_executed_at,
            'created_at': workflow.created_at,
            'updated_at': workflow.updated_at,
        })
    
    elif request.method == 'PUT':
        data = request.data
        
        # VERSIONING: Create a snapshot of the current state before update
        # This ensures "Never mutate without versioning" rule
        last_version = WorkflowVersion.objects.filter(workflow=workflow).order_by('-version_number').first()
        next_version_num = (last_version.version_number + 1) if last_version else 1
        
        # Limit version history to 10 versions
        MAX_VERSIONS = 10
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

        # Update fields
        if 'name' in data:
            workflow.name = data['name']
        if 'description' in data:
            workflow.description = data['description']
        if 'nodes' in data:
            workflow.nodes = data['nodes']
        if 'edges' in data:
            workflow.edges = data['edges']
        if 'viewport' in data:
            workflow.viewport = data['viewport']
        if 'workflow_settings' in data:
            workflow.workflow_settings = data['workflow_settings']
        if 'status' in data:
            new_status = data['status']
            if new_status == 'active' and workflow.status != 'active':
                # Run validations before allowing deployment
                temp_nodes = data.get('nodes', workflow.nodes)
                temp_edges = data.get('edges', workflow.edges)
                
                # 1. Static Validation
                errors = []
                errors.extend(validate_dag(temp_nodes, temp_edges))
                errors.extend(validate_node_configs(temp_nodes))
                
                from credentials.models import Credential
                user_credentials = set(Credential.objects.filter(user=request.user).values_list('name', flat=True))
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

            workflow.status = new_status
            
            # Trigger Lifecycle Management
            try:
                mgr = get_trigger_manager()
                if new_status == 'active':
                    # Only register if we're moving TO active
                    mgr.register_triggers(workflow)
                elif new_status in ('draft', 'paused', 'archived'):
                    # Clear triggers if moving FROM active
                    mgr.unregister_triggers(workflow.id)
            except Exception as e:
                # Log but don't fail the request if trigger registration fails
                logger.error(f"Failed to manage triggers for workflow {workflow.id}: {e}")

        if 'icon' in data:
            workflow.icon = data['icon']
        if 'color' in data:
            workflow.color = data['color']
        if 'tags' in data:
            workflow.tags = data['tags']
        if 'supervision_level' in data:
            # Validate supervision level
            valid_levels = ['error_only', 'full', 'none']
            if data['supervision_level'] in valid_levels:
                workflow.supervision_level = data['supervision_level']
        
        # LLM provider settings
        if 'llm_provider' in data:
            valid_providers = ['openrouter', 'openai', 'gemini', 'ollama', 'perplexity']
            if data['llm_provider'] in valid_providers:
                workflow.llm_provider = data['llm_provider']
        if 'llm_model' in data:
            workflow.llm_model = data['llm_model']
        if 'llm_credential_id' in data:
            from credentials.models import Credential
            cred_id = data['llm_credential_id']
            if cred_id:
                # Validate credential belongs to user
                try:
                    cred = Credential.objects.get(id=cred_id, user=request.user)
                    workflow.llm_credential = cred
                except Credential.DoesNotExist:
                    pass  # Ignore invalid credential
            else:
                workflow.llm_credential = None
        
        workflow.save()
        
        return Response({
            'id': workflow.id,
            'name': workflow.name,
            'updated_at': workflow.updated_at,
            'version_created': next_version_num
        })
    
    elif request.method == 'DELETE':
        workflow.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


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
    
    # Auto-inject user credentials
    from executor.credential_utils import get_user_credentials
    user_credentials = await sync_to_async(get_user_credentials)(request.user.id)
    
    # Build workflow JSON
    workflow_json = {
        'id': workflow.id,
        'nodes': workflow.nodes,
        'edges': workflow.edges,
        'settings': workflow.workflow_settings,
    }
    
    # Start execution
    handle = await orchestrator.start(
        workflow_json=workflow_json,
        user_id=request.user.id,
        input_data=input_data,
        credentials=user_credentials,
        supervision=workflow.supervision_level,  # Use workflow's setting
    )
    
    return Response({
        'execution_id': str(handle.execution_id),
        'workflow_id': workflow.id,
        'state': handle.state.value,
        'started_at': handle.started_at,
    })


@api_view(['POST'])
@permission_classes([IsAuthenticated])
async def pause_execution(request, execution_id: str):
    """Pause a running execution."""
    from executor.king import get_orchestrator
    
    orchestrator = get_orchestrator(request.user.id)
    result = await orchestrator.pause(UUID(execution_id))
    
    if result:
        return Response({'status': 'paused', 'execution_id': execution_id})
    return Response({'error': 'Could not pause execution'}, status=400)


@api_view(['POST'])
@permission_classes([IsAuthenticated])
async def resume_execution(request, execution_id: str):
    """Resume a paused execution."""
    from executor.king import get_orchestrator
    
    orchestrator = get_orchestrator(request.user.id)
    result = await orchestrator.resume(UUID(execution_id))
    
    if result:
        return Response({'status': 'resumed', 'execution_id': execution_id})
    return Response({'error': 'Could not resume execution'}, status=400)


@api_view(['POST'])
@permission_classes([IsAuthenticated])
async def stop_execution(request, execution_id: str):
    """Stop/cancel an execution."""
    from executor.king import get_orchestrator
    
    orchestrator = get_orchestrator(request.user.id)
    result = await orchestrator.stop(UUID(execution_id))
    
    if result:
        return Response({'status': 'stopped', 'execution_id': execution_id})
    return Response({'error': 'Could not stop execution'}, status=400)


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def execution_status(request, execution_id: str):
    """Get current execution status."""
    from executor.king import get_orchestrator
    
    orchestrator = get_orchestrator(request.user.id)
    handle = orchestrator.get_status(UUID(execution_id))
    
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
    
    result = [
        {
            'request_id': str(r.request_id),
            'type': r.request_type,
            'title': r.title,
            'message': r.message,
            'options': r.options,
            'node_id': r.node_id,
            'execution_id': str(r.execution.execution_id) if r.execution else None,
            'timeout_seconds': r.timeout_seconds,
            'created_at': r.created_at,
        }
        for r in requests
    ]
    
    return Response({'pending': result, 'count': len(result)})


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
def conversation_messages(request, conversation_id: str = None):
    """
    GET: Get conversation history
    POST: Add a message (and get AI response)
    DELETE: Delete a conversation
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
        ).order_by('created_at').values('role', 'content', 'metadata', 'created_at')
        
        return Response({'messages': list(messages)})
    
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
            'user_message': {'content': content, 'created_at': user_msg.created_at},
            'ai_response': {'content': ai_response, 'created_at': ai_msg.created_at},
        })
    
    elif request.method == 'DELETE':
        if not conversation_id:
            return Response({'error': 'Conversation ID required'}, status=400)
            
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
        versions = list(
            WorkflowVersion.objects
            .filter(workflow=workflow)
            .order_by('-version_number')
            .values('id', 'version_number', 'label', 'change_summary', 'created_at')
        )
        return Response({'versions': versions})
    
    elif request.method == 'POST':
        # Get next version number
        last_version = WorkflowVersion.objects.filter(workflow=workflow).order_by('-version_number').first()
        next_version = (last_version.version_number + 1) if last_version else 1
        
        # Limit version history to 10 versions
        MAX_VERSIONS = 10
        current_versions = WorkflowVersion.objects.filter(workflow=workflow).order_by('version_number')
        if current_versions.count() >= MAX_VERSIONS:
            # Delete oldest versions to keep total at MAX_VERSIONS after new one
            to_delete_count = current_versions.count() - MAX_VERSIONS + 1
            if to_delete_count > 0:
                to_delete_ids = list(current_versions.values_list('id', flat=True)[:to_delete_count])
                WorkflowVersion.objects.filter(id__in=to_delete_ids).delete()

        version = WorkflowVersion.objects.create(
            workflow=workflow,
            version_number=next_version,
            label=request.data.get('label', ''),
            nodes=workflow.nodes,
            edges=workflow.edges,
            workflow_settings=workflow.workflow_settings,
            created_by=request.user,
            change_summary=request.data.get('change_summary', ''),
        )
        
        return Response({
            'id': version.id,
            'version_number': version.version_number,
            'created_at': version.created_at,
        }, status=status.HTTP_201_CREATED)


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
    
    if not description:
        return Response({'error': 'Description is required'}, status=400)

    from executor.king import get_orchestrator
    orchestrator = get_orchestrator(request.user.id)
    
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
    
    if not modification:
        return Response({'error': 'Modification description is required'}, status=400)
    
    current_workflow = {
        'name': workflow.name,
        'description': workflow.description,
        'nodes': workflow.nodes,
        'edges': workflow.edges,
    }
    
    from executor.king import get_orchestrator
    orchestrator = get_orchestrator(request.user.id)
    
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
    
    if not message:
        return Response({'error': 'Message is required'}, status=400)
    
    chat = ContextAwareChat(user_id=request.user.id)
    
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

    node_id = request.data.get('node_id')
    node_type = request.data.get('node_type')
    input_data = request.data.get('input_data', {})
    config = request.data.get('config', {})
    
    if not node_id or not node_type:
        logger.error(f"Partial execution missing required fields: node_id={node_id}, node_type={node_type}")
        return Response({'error': 'node_id and node_type are required'}, status=400)
    
    registry = get_registry()
    
    if not registry.has_handler(node_type):
        logger.error(f"Partial execution unknown node type: {node_type}")
        return Response({'error': f'Unknown node type: {node_type}'}, status=400)
    
    handler = registry.get_handler(node_type)
    
    logger.info(f"Executing partial node: {node_type} ({node_id})")
    
    # Create context
    context = ExecutionContext(
        execution_id=uuid4(),
        user_id=request.user.id,
        workflow_id=workflow_id or 0,
        node_id=node_id
    )
    
    try:
        result = await handler.execute(input_data, config, context)
        
        if result.success:
            # Return items array in n8n-compatible format
            items = [item.model_dump() for item in result.items]
            return Response({'items': items})
        else:
            return Response({'error': result.error}, status=400)
            
    except Exception as e:
        import traceback
        logger.error(f"Partial execution failed: {e}\n{traceback.format_exc()}")
        return Response({'error': str(e)}, status=500)


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

    # 2. Validate Authentication (Simple)
    auth_type = config.get("authentication", "none")
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
    input_data = {
        "headers": dict(request.headers),
        "body": body_data,
        "query": dict(request.GET),
        "method": request.method,
        "url": request.build_absolute_uri(),
        "trigger_type": "webhook",
    }

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

