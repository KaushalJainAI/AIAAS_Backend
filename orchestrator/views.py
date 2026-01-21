"""
Orchestrator App API Views

Workflow CRUD, execution control, and HITL endpoints.
"""
from uuid import UUID

from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from .models import Workflow, WorkflowVersion, HITLRequest, ConversationMessage


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
        
        workflow = Workflow.objects.create(
            user=request.user,
            name=data.get('name', 'Untitled Workflow'),
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
            workflow.status = data['status']
        if 'icon' in data:
            workflow.icon = data['icon']
        if 'color' in data:
            workflow.color = data['color']
        if 'tags' in data:
            workflow.tags = data['tags']
        
        workflow.save()
        
        return Response({
            'id': workflow.id,
            'name': workflow.name,
            'updated_at': workflow.updated_at,
        })
    
    elif request.method == 'DELETE':
        workflow.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


# ======================== Execution Control API ========================

@api_view(['POST'])
@permission_classes([IsAuthenticated])
def execute_workflow(request, workflow_id: int):
    """
    Start executing a workflow.
    
    Request body:
        - input_data: Initial input data (optional)
        - async: Return immediately with execution_id (default: true)
    """
    from executor.orchestrator import get_orchestrator
    import asyncio
    
    workflow = get_object_or_404(Workflow, id=workflow_id, user=request.user)
    
    input_data = request.data.get('input_data', {})
    is_async = request.data.get('async', True)
    
    # Get orchestrator
    orchestrator = get_orchestrator()
    
    # Build workflow JSON
    workflow_json = {
        'id': workflow.id,
        'nodes': workflow.nodes,
        'edges': workflow.edges,
        'settings': workflow.workflow_settings,
    }
    
    # Start execution
    async def start():
        return await orchestrator.start(
            workflow_json=workflow_json,
            user_id=request.user.id,
            input_data=input_data,
        )
    
    handle = asyncio.run(start())
    
    return Response({
        'execution_id': str(handle.execution_id),
        'workflow_id': workflow.id,
        'state': handle.state.value,
        'started_at': handle.started_at,
    })


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def pause_execution(request, execution_id: str):
    """Pause a running execution."""
    from executor.orchestrator import get_orchestrator
    import asyncio
    
    orchestrator = get_orchestrator()
    result = asyncio.run(orchestrator.pause(UUID(execution_id)))
    
    if result:
        return Response({'status': 'paused', 'execution_id': execution_id})
    return Response({'error': 'Could not pause execution'}, status=400)


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def resume_execution(request, execution_id: str):
    """Resume a paused execution."""
    from executor.orchestrator import get_orchestrator
    import asyncio
    
    orchestrator = get_orchestrator()
    result = asyncio.run(orchestrator.resume(UUID(execution_id)))
    
    if result:
        return Response({'status': 'resumed', 'execution_id': execution_id})
    return Response({'error': 'Could not resume execution'}, status=400)


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def stop_execution(request, execution_id: str):
    """Stop/cancel an execution."""
    from executor.orchestrator import get_orchestrator
    import asyncio
    
    orchestrator = get_orchestrator()
    result = asyncio.run(orchestrator.stop(UUID(execution_id)))
    
    if result:
        return Response({'status': 'stopped', 'execution_id': execution_id})
    return Response({'error': 'Could not stop execution'}, status=400)


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def execution_status(request, execution_id: str):
    """Get current execution status."""
    from executor.orchestrator import get_orchestrator
    
    orchestrator = get_orchestrator()
    handle = orchestrator.get_status(UUID(execution_id))
    
    if not handle:
        return Response({'error': 'Execution not found'}, status=404)
    
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
def respond_to_hitl(request, request_id: str):
    """
    Respond to a HITL request.
    
    Request body:
        - action: 'approve', 'reject', 'answer', 'skip', 'retry'
        - value: Response value (for clarification)
        - message: Optional message
    """
    from executor.orchestrator import get_orchestrator
    import asyncio
    
    try:
        hitl_request = HITLRequest.objects.get(
            request_id=request_id,
            user=request.user,
            status='pending'
        )
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
    hitl_request.save()
    
    # Notify orchestrator
    orchestrator = get_orchestrator()
    asyncio.run(orchestrator.respond_to_hitl(
        request_id=request_id,
        response={'action': action, 'value': value},
    ))
    
    return Response({
        'request_id': request_id,
        'status': hitl_request.status,
        'responded_at': hitl_request.responded_at,
    })


# ======================== AI Chat API ========================

@api_view(['GET', 'POST'])
@permission_classes([IsAuthenticated])
def conversation_messages(request, conversation_id: str = None):
    """
    GET: Get conversation history
    POST: Add a message (and get AI response)
    """
    from uuid import uuid4
    
    if request.method == 'GET':
        if not conversation_id:
            # List recent conversations
            conversations = (
                ConversationMessage.objects
                .filter(user=request.user)
                .values('conversation_id')
                .distinct()
                .order_by('-created_at')[:20]
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
def generate_workflow(request):
    """
    Generate a workflow from natural language description.
    
    Request body:
        - description: What the workflow should do
        - credential_id: LLM credential ID (optional)
    """
    import asyncio
    from .ai_generator import get_ai_generator
    
    description = request.data.get('description', '')
    credential_id = request.data.get('credential_id')
    
    if not description:
        return Response({'error': 'Description is required'}, status=400)
    
    generator = get_ai_generator()
    
    async def generate():
        return await generator.generate(
            description=description,
            user_id=request.user.id,
            credential_id=credential_id,
        )
    
    result = asyncio.run(generate())
    
    if 'error' in result:
        return Response(result, status=400)
    
    # Optionally save the generated workflow
    if request.data.get('save', False):
        workflow = Workflow.objects.create(
            user=request.user,
            name=result.get('name', 'Generated Workflow'),
            description=result.get('description', description),
            nodes=result.get('nodes', []),
            edges=result.get('edges', []),
            status='draft',
        )
        result['saved'] = True
        result['workflow_id'] = workflow.id
    
    return Response(result)


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def modify_workflow(request, workflow_id: int):
    """
    Modify a workflow using natural language.
    
    Request body:
        - modification: What to change
        - credential_id: LLM credential ID (optional)
    """
    import asyncio
    from .ai_generator import get_ai_generator
    
    workflow = get_object_or_404(Workflow, id=workflow_id, user=request.user)
    modification = request.data.get('modification', '')
    credential_id = request.data.get('credential_id')
    
    if not modification:
        return Response({'error': 'Modification description is required'}, status=400)
    
    current_workflow = {
        'name': workflow.name,
        'description': workflow.description,
        'nodes': workflow.nodes,
        'edges': workflow.edges,
    }
    
    generator = get_ai_generator()
    
    async def modify():
        return await generator.modify(
            workflow=current_workflow,
            modification=modification,
            user_id=request.user.id,
            credential_id=credential_id,
        )
    
    result = asyncio.run(modify())
    
    if 'error' in result:
        return Response(result, status=400)
    
    # Apply changes if requested
    if request.data.get('apply', False):
        workflow.name = result.get('name', workflow.name)
        workflow.description = result.get('description', workflow.description)
        workflow.nodes = result.get('nodes', workflow.nodes)
        workflow.edges = result.get('edges', workflow.edges)
        workflow.save()
        result['applied'] = True
    
    return Response(result)


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def suggest_improvements(request, workflow_id: int):
    """Get AI suggestions for workflow improvements."""
    import asyncio
    from .ai_generator import get_ai_generator
    
    workflow = get_object_or_404(Workflow, id=workflow_id, user=request.user)
    credential_id = request.query_params.get('credential_id')
    
    current_workflow = {
        'name': workflow.name,
        'nodes': workflow.nodes,
        'edges': workflow.edges,
    }
    
    generator = get_ai_generator()
    
    async def suggest():
        return await generator.suggest_improvements(
            workflow=current_workflow,
            user_id=request.user.id,
            credential_id=credential_id,
        )
    
    suggestions = asyncio.run(suggest())
    
    return Response({
        'workflow_id': workflow_id,
        'suggestions': suggestions,
    })


# ======================== Context-Aware Chat API ========================

@api_view(['POST'])
@permission_classes([IsAuthenticated])
def context_aware_chat(request):
    """
    Send a context-aware chat message.
    
    Request body:
        - message: User's message
        - workflow_id: Optional workflow context
        - node_id: Optional focused node
        - conversation_id: Optional conversation ID
        - credential_id: LLM credential ID
    """
    import asyncio
    from .chat_context import ContextAwareChat
    
    message = request.data.get('message', '')
    workflow_id = request.data.get('workflow_id')
    node_id = request.data.get('node_id')
    conversation_id = request.data.get('conversation_id')
    credential_id = request.data.get('credential_id')
    
    if not message:
        return Response({'error': 'Message is required'}, status=400)
    
    chat = ContextAwareChat(user_id=request.user.id)
    
    async def send():
        return await chat.send_message(
            message=message,
            workflow_id=workflow_id,
            node_id=node_id,
            conversation_id=conversation_id,
            credential_id=credential_id,
        )
    
    result = asyncio.run(send())
    
    return Response(result)


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

