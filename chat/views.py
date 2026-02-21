from django.shortcuts import get_object_or_404
from rest_framework import status, viewsets
from adrf.decorators import api_view
from rest_framework.decorators import permission_classes, action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from uuid import UUID

from .models import ChatSession, ChatMessage
from .serializers import ChatSessionSerializer, ChatMessageSerializer
import logging

logger = logging.getLogger(__name__)

class ChatSessionViewSet(viewsets.ModelViewSet):
    """
    ViewSet for managing standalone chat sessions.
    """
    serializer_class = ChatSessionSerializer
    permission_classes = [IsAuthenticated]
    
    def get_queryset(self):
        return ChatSession.objects.filter(user=self.request.user)
        
    def perform_create(self, serializer):
        serializer.save(user=self.request.user)


@api_view(['POST'])
@permission_classes([IsAuthenticated])
async def send_message(request, session_id: str):
    """
    Send a message to a standalone chat session and get an AI response.
    """
    try:
        session_uuid = UUID(session_id)
        session = await ChatSession.objects.filter(id=session_uuid, user=request.user).afirst()
    except ValueError:
        return Response({'error': 'Invalid session ID format'}, status=400)
        
    if not session:
        return Response({'error': 'Chat session not found'}, status=404)
        
    content = request.data.get('content')
    if not content:
        return Response({'error': 'Content is required'}, status=400)
        
    # Save the user's message
    user_msg = await ChatMessage.objects.acreate(
        session=session,
        role='user',
        content=content
    )
    
    # Simple integration with LLM
    from nodes.handlers.registry import get_registry
    from compiler.schemas import ExecutionContext
    from uuid import uuid4
    
    registry = get_registry()
    provider = session.llm_provider
    model = session.llm_model
    
    if not registry.has_handler(provider):
        # Fallback to a placeholder response if no provider is configured
        ai_msg = await ChatMessage.objects.acreate(
            session=session,
            role='assistant',
            content=f"Error: Provider {provider} not available in registry."
        )
        return Response({
            'user_message': ChatMessageSerializer(user_msg).data,
            'ai_response': ChatMessageSerializer(ai_msg).data,
        })
        
    handler = registry.get_handler(provider)
    
    # Build prompt with sliding window history
    # For MVP, just send system prompt and current message
    prompt_text = ""
    if session.system_prompt:
        prompt_text += f"{session.system_prompt}\n\n"
    prompt_text += f"User: {content}\nAssistant:"
    
    context = ExecutionContext(
        execution_id=uuid4(),
        user_id=request.user.id,
        workflow_id=0
    )
    
    config = {
        "prompt": prompt_text,
        "model": model,
        "temperature": 0.7,
    }
    
    try:
        # Note: In a true Gemini UI, this would be a StreamingHttpResponse using SSE. 
        # Making a standard blocking call for the MVP.
        result = await handler.execute({}, config, context)
        
        if result.success:
            ai_content = result.data.get("content", "")
            tokens = result.data.get("usage", {}).get("total_tokens", 0)
        else:
            ai_content = f"Failed to generate response: {result.error}"
            tokens = 0
            
    except Exception as e:
        logger.exception(f"Chat generation failed: {e}")
        ai_content = f"Internal Error: {str(e)}"
        tokens = 0
        
    # Save AI response
    ai_msg = await ChatMessage.objects.acreate(
        session=session,
        role='assistant',
        content=ai_content,
        metadata={'tokens': tokens, 'model': model}
    )
    
    return Response({
        'user_message': ChatMessageSerializer(user_msg).data,
        'ai_response': ChatMessageSerializer(ai_msg).data,
    })
