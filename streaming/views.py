"""
Streaming Views - SSE and API Endpoints for Real-time Updates

Provides HTTP endpoints for:
- SSE streaming of execution events
- Stream history/replay
- Connection management
"""
import asyncio
import logging
from uuid import UUID

from django.http import StreamingHttpResponse, JsonResponse
from rest_framework import status
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated

from logs.models import ExecutionLog
from .broadcaster import get_broadcaster, StreamEvent
from .models import StreamEvent as StreamEventModel
from core.throttling import StreamThrottle

logger = logging.getLogger(__name__)


class ExecutionStreamView(APIView):
    """
    SSE endpoint for streaming execution events.
    
    GET /api/streaming/executions/<execution_id>/stream/
    
    Returns a Server-Sent Events stream with real-time updates.
    Client should use EventSource API to consume.
    """
    
    permission_classes = [IsAuthenticated]
    throttle_classes = [StreamThrottle]
    
    def get(self, request, execution_id: UUID):
        """Stream execution events via SSE."""
        # Verify user owns this execution
        try:
            execution = ExecutionLog.objects.get(
                execution_id=execution_id,
                user=request.user
            )
        except ExecutionLog.DoesNotExist:
            return Response(
                {'error': 'Execution not found'},
                status=status.HTTP_404_NOT_FOUND
            )
        
        # Track connection
        StreamThrottle.increment_connection(request.user.pk)
        
        async def event_generator():
            """Generate SSE events."""
            broadcaster = get_broadcaster()
            
            try:
                async for event in broadcaster.stream_execution(
                    str(execution_id),
                    timeout=300,  # 5 minutes
                    heartbeat_interval=30
                ):
                    yield event
            finally:
                # Decrement connection on disconnect
                StreamThrottle.decrement_connection(request.user.pk)
        
        # Use async generator for streaming
        response = StreamingHttpResponse(
            event_generator(),
            content_type='text/event-stream'
        )
        
        # SSE headers
        response['Cache-Control'] = 'no-cache'
        response['X-Accel-Buffering'] = 'no'  # Disable nginx buffering
        response['Connection'] = 'keep-alive'
        
        return response


class ExecutionEventsHistoryView(APIView):
    """
    Get historical events for an execution.
    
    GET /api/streaming/executions/<execution_id>/events/
    
    Query params:
        - after_sequence: Only return events after this sequence number
        - limit: Maximum events to return (default 100)
    """
    
    permission_classes = [IsAuthenticated]
    
    def get(self, request, execution_id: UUID):
        """Get execution event history."""
        # Verify user owns this execution
        try:
            execution = ExecutionLog.objects.get(
                execution_id=execution_id,
                user=request.user
            )
        except ExecutionLog.DoesNotExist:
            return Response(
                {'error': 'Execution not found'},
                status=status.HTTP_404_NOT_FOUND
            )
        
        # Get query params
        after_sequence = request.query_params.get('after_sequence', 0)
        limit = min(int(request.query_params.get('limit', 100)), 500)
        
        try:
            after_sequence = int(after_sequence)
        except ValueError:
            after_sequence = 0
        
        # Get events from database
        events = StreamEventModel.objects.filter(
            execution__execution_id=execution_id,
            sequence__gt=after_sequence
        ).order_by('sequence')[:limit]
        
        return Response({
            'execution_id': str(execution_id),
            'events': [
                {
                    'id': str(e.event_id),
                    'type': e.event_type,
                    'data': e.data,
                    'node_id': e.node_id,
                    'sequence': e.sequence,
                    'timestamp': e.created_at.isoformat(),
                }
                for e in events
            ],
            'has_more': events.count() == limit
        })


class StreamConnectionStatusView(APIView):
    """
    Get current streaming connection status.
    
    GET /api/streaming/status/
    
    Returns user's current connection count and limits.
    """
    
    permission_classes = [IsAuthenticated]
    
    def get(self, request):
        """Get connection status."""
        from django.core.cache import cache
        
        user = request.user
        cache_key = f'stream_connections_{user.pk}'
        current_connections = cache.get(cache_key, 0)
        
        # Get limit from profile
        try:
            tier = user.profile.tier
        except AttributeError:
            tier = 'free'
        
        limits = {
            'free': 5,
            'pro': 20,
            'enterprise': 100,
        }
        
        return Response({
            'current_connections': current_connections,
            'max_connections': limits.get(tier, 5),
            'tier': tier,
        })


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def test_stream_event(request, execution_id: UUID):
    """
    Test endpoint to manually send a stream event.
    
    POST /api/streaming/executions/<execution_id>/test/
    
    Body:
        {
            "event_type": "node_start",
            "data": {"node_id": "test", ...}
        }
    
    Only available in DEBUG mode.
    """
    from django.conf import settings
    
    if not settings.DEBUG:
        return Response(
            {'error': 'Test endpoint only available in DEBUG mode'},
            status=status.HTTP_403_FORBIDDEN
        )
    
    # Verify user owns this execution
    try:
        execution = ExecutionLog.objects.get(
            execution_id=execution_id,
            user=request.user
        )
    except ExecutionLog.DoesNotExist:
        return Response(
            {'error': 'Execution not found'},
            status=status.HTTP_404_NOT_FOUND
        )
    
    event_type = request.data.get('event_type', 'test')
    data = request.data.get('data', {})
    
    # Send event
    from .broadcaster import send_event_sync
    send_event_sync(str(execution_id), event_type, data)
    
    return Response({'status': 'sent', 'event_type': event_type})
