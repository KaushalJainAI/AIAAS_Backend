"""
The builder chat: stored messages and the conversation transcript.
"""

import logging

from adrf.decorators import api_view
from drf_spectacular.utils import extend_schema, inline_serializer
from rest_framework import serializers as drf_serializers
from rest_framework import status
from rest_framework.decorators import permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from ..models import ConversationMessage, SubAgent
from ..serializers import ConversationMessageSerializer
from .responses import _ErrorResponse

logger = logging.getLogger(__name__)


@extend_schema(
    methods=['GET'],
    responses={
        200: inline_serializer(
            name="ConversationListOrMessages",
            fields={
                "conversations": drf_serializers.ListField(child=drf_serializers.DictField(), required=False),
                "messages": ConversationMessageSerializer(many=True, required=False),
            },
        ),
    },
)
@extend_schema(
    methods=['POST'],
    request=inline_serializer(
        name="ConversationPostRequest",
        fields={
            "content": drf_serializers.CharField(),
            "workflow_id": drf_serializers.IntegerField(required=False, allow_null=True),
        },
    ),
    responses={
        202: inline_serializer(
            name="ConversationPostResponse",
            fields={
                "conversation_id": drf_serializers.CharField(),
                "user_message": drf_serializers.DictField(),
                "detail": drf_serializers.CharField(),
            },
        ),
    },
)
@extend_schema(
    methods=['DELETE'],
    responses={
        200: inline_serializer(
            name="ConversationDeleteResponse",
            fields={"deleted": drf_serializers.IntegerField(required=False),
                    "status": drf_serializers.CharField(required=False)},
        ),
        400: _ErrorResponse,
        404: _ErrorResponse,
    },
)
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
        # NOTE: POST persists the user message only — reply generation is not
        # wired up, so this returns a 202 without an assistant reply. Wire real
        # generation here if the builder assistant ships.
        content = request.data.get('content', '')
        conv_id = conversation_id or str(uuid4())

        # The column is `subagent`; `workflow_id` is kept as the wire name
        # because the frontend already sends it. Re-scoped to the caller rather
        # than trusted: an id pointing at someone else's agent would attach this
        # conversation to a row they own. An unknown or unowned id stores null
        # rather than 500-ing — the message itself is still worth keeping.
        agent_id = request.data.get('subagent_id', request.data.get('workflow_id'))
        subagent_id = None
        try:
            agent_id = int(agent_id) if agent_id not in (None, '') else None
        except (TypeError, ValueError):
            agent_id = None
        if agent_id is not None:
            subagent_id = (
                SubAgent.objects
                .filter(id=agent_id, user=request.user)
                .values_list('id', flat=True)
                .first()
            )

        # Save user message
        user_msg = ConversationMessage.objects.create(
            user=request.user,
            conversation_id=conv_id,
            subagent_id=subagent_id,
            role='user',
            content=content,
        )

        return Response({
            'conversation_id': conv_id,
            'user_message': {'id': user_msg.id, 'content': content, 'created_at': user_msg.created_at},
            'detail': 'Workflow chat response generation is not configured.',
        }, status=202)
    
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


