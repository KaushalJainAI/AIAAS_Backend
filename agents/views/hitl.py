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
    Answer a paused run from the Inbox, and let it carry on.

    Request body:
        - action: 'approve', 'reject', 'answer', 'skip', 'retry'
        - value / response: Response value (for clarification)
        - message: Optional message
        - scope: 'once' | 'session' | 'always' for an approval

    Until this resumed anything, the Inbox was a dead end that looked like a
    working screen: the row was marked answered, 200 came back, the reminder
    ladder stood down — and the run stayed parked on its `interrupt()` with
    nothing left to notice. The view even said so, deferring to
    `agents/{id}/approve/`, which the Inbox has never called.

    So this is the same three steps that route already runs, in the same order
    and through the same functions. It is not a second way to approve: it
    resolves the row into a `(thread_id, call_id)` and then does exactly what
    `agent_approve` / `agent_reject` do, because a second write path is a
    second place for the ownership check and the resume to drift apart.
    """
    from django.utils import timezone

    hitl_request = await HITLRequest.objects.filter(
        request_id=request_id,
        user=request.user,
        status='pending',
    ).select_related('execution__subagent').afirst()
    if hitl_request is None:
        # Also the answer when the live socket got there first. The filter on
        # `status='pending'` is what makes answering twice harmless rather
        # than a second resume of a run that is already going.
        return Response({'error': 'Request not found or already responded'}, status=404)

    action = request.data.get('action', 'approve')
    value = request.data.get('value', request.data.get('response'))
    message = request.data.get('message', '')

    if action in ('approve', 'approved'):
        resolution = 'approved'
    elif action in ('reject', 'rejected', 'stop'):
        resolution = 'rejected'
    else:
        resolution = 'answered'

    # Record the decision in the checkpoint *before* the row is closed, exactly
    # as `agent_approve` does. Both halves are optional: a `clarification` row,
    # or one written before `context_data` carried a thread id, still has to
    # close cleanly rather than 500 on a key that was never there.
    context = hitl_request.context_data or {}
    thread_id = (context.get('thread_id') or '').strip()
    call_id = hitl_request.node_id or context.get('call_id') or ''
    agent = getattr(hitl_request.execution, 'subagent', None)

    resumable = bool(thread_id and call_id and agent is not None)
    if resumable:
        from chat.turn.agent import approve_tool_call, reject_tool_call

        if resolution == 'approved':
            await approve_tool_call(
                thread_id, call_id,
                scope=str(request.data.get('scope') or 'once'),
                # An agent run's thread id *is* its session id. Passed
                # explicitly rather than relied on, as in `agent_approve`.
                session_key=thread_id,
                user_id=request.user.id,
            )
        elif resolution == 'rejected':
            resumable = await reject_tool_call(
                thread_id, call_id,
                reason=str(message or value or ''),
            )
        else:
            # An answer to a clarification is not a tool decision; there is
            # nothing to write into the checkpoint for it.
            resumable = False

    hitl_request.status = resolution
    hitl_request.response = {
        'action': action,
        'value': value,
        'message': message,
    }
    hitl_request.responded_at = timezone.now()
    await hitl_request.asave(
        update_fields=['status', 'response', 'responded_at', 'updated_at']
    )

    execution_id = None
    if resumable:
        from agents.agent.runtime import resume_agent_run

        try:
            execution_id = await resume_agent_run(
                agent, user=request.user, thread_id=thread_id
            )
        except Exception:  # noqa: BLE001
            # The answer is recorded either way. Failing the request here would
            # tell the user their decision did not land when it did, and they
            # have no second copy of it to send.
            logger.exception('[HITL] Could not resume run for request %s', request_id)

    return Response({
        'request_id': request_id,
        'status': hitl_request.status,
        'responded_at': hitl_request.responded_at,
        'execution_id': execution_id,
        'resumed': execution_id is not None,
    })
