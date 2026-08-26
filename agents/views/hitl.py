"""
Human-in-the-loop: the pending queue and the response that resumes a run.
"""

import logging

from adrf.decorators import api_view
from rest_framework.decorators import permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from ..models import HITLRequest
from ..serializers import HITLRequestSerializer

logger = logging.getLogger(__name__)


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def pending_hitl_requests(request):
    """Get all pending HITL requests for the user."""
    requests = HITLRequest.objects.filter(
        user=request.user,
        status='pending'
    ).select_related('execution__subagent').order_by('-created_at')
    
    serializer = HITLRequestSerializer(requests, many=True)
    # The frontend reads `.requests` (useHitlPending → getPendingHITL); `pending`
    # is kept as an alias so nothing that already read it breaks.
    return Response({
        'requests': serializer.data,
        'pending': serializer.data,
        'count': len(serializer.data),
    })


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
    
    # The persisted row above is the whole of the response. Agent approvals
    # resume the run through `agents/{id}/approve/`.

    return Response({
        'request_id': request_id,
        'status': hitl_request.status,
        'responded_at': hitl_request.responded_at,
    })
