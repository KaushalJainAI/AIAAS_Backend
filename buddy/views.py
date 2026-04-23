from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework import status

@api_view(['POST'])
@permission_classes([IsAuthenticated])
def process_context(request):
    """
    Endpoint for the help assistant to receive current frontend context.
    The payload should include what is currently on the screen.
    """
    # For now, just echo back the received context to acknowledge it.
    # Later, we can connect this to an LLM or specific agent logic.
    context_data = request.data.get('context', {})
    
    # Placeholder logic
    return Response({
        'status': 'success',
        'message': 'Context received',
        'received_context': context_data
    }, status=status.HTTP_200_OK)

@api_view(['POST'])
@permission_classes([IsAuthenticated])
def trigger_action(request):
    """
    Endpoint for the help assistant to issue commands to the frontend.
    The payload should contain the action type and parameters.
    """
    action_type = request.data.get('action_type')
    action_params = request.data.get('parameters', {})
    
    if not action_type:
        return Response({
            'status': 'error',
            'message': 'action_type is required'
        }, status=status.HTTP_400_BAD_REQUEST)
        
    # Placeholder logic for handling actions
    # This could emit a WebSocket event to the frontend or be polled.
    return Response({
        'status': 'success',
        'message': f'Action {action_type} triggered',
        'action_details': {
            'type': action_type,
            'params': action_params
        }
    }, status=status.HTTP_200_OK)
