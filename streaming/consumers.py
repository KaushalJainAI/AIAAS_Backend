"""
WebSocket Consumers - Real-time Communication for HITL and Execution

Django Channels consumers for:
- Execution updates (real-time node progress)
- HITL requests/responses (approval, clarification, error recovery)
- Orchestrator communication

Usage:
    # In frontend
    ws = new WebSocket('ws://localhost:8000/ws/execution/<execution_id>/');
    ws.onmessage = (e) => console.log(JSON.parse(e.data));
    
    # Respond to HITL
    ws.send(JSON.stringify({type: 'hitl_response', request_id: '...', response: {...}}));
"""
import json
import logging
from datetime import datetime
from uuid import UUID
from typing import Optional

from channels.generic.websocket import AsyncWebsocketConsumer
from channels.db import database_sync_to_async
from django.utils import timezone

logger = logging.getLogger(__name__)


class ExecutionConsumer(AsyncWebsocketConsumer):
    """
    WebSocket consumer for execution updates and HITL.
    
    Groups:
        - execution_{execution_id}: Execution-specific events
        - user_{user_id}: User-wide notifications
    
    Message types (server -> client):
        - execution.event: Node/workflow events
        - hitl.request: HITL approval/clarification needed
        - error: Error notifications
    
    Message types (client -> server):
        - hitl_response: Response to HITL request
        - subscribe: Subscribe to additional executions
        - unsubscribe: Unsubscribe from execution
    """
    
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.execution_id: Optional[str] = None
        self.user_id: Optional[int] = None
        self.groups: list[str] = []
    
    async def connect(self):
        """Handle WebSocket connection."""
        # Get execution_id from URL
        self.execution_id = self.scope['url_route']['kwargs'].get('execution_id')
        
        # Get user from scope
        user = self.scope.get('user')
        if not user or not user.is_authenticated:
            # Reject unauthenticated connections
            await self.close(code=4001)
            return
        
        self.user_id = user.pk
        
        # Verify user has access to this execution
        if self.execution_id:
            has_access = await self._verify_execution_access(self.execution_id)
            if not has_access:
                await self.close(code=4003)
                return
            
            # Join execution group
            group_name = f"execution_{self.execution_id}"
            await self.channel_layer.group_add(group_name, self.channel_name)
            self.groups.append(group_name)
        
        # Join user group (for user-wide notifications)
        user_group = f"user_{self.user_id}"
        await self.channel_layer.group_add(user_group, self.channel_name)
        self.groups.append(user_group)
        
        await self.accept()
        
        # Send connection confirmation
        await self.send_json({
            'type': 'connected',
            'data': {
                'execution_id': self.execution_id,
                'user_id': self.user_id,
                'timestamp': datetime.utcnow().isoformat(),
            }
        })
        
        logger.info(f"WebSocket connected: user={self.user_id}, execution={self.execution_id}")
    
    async def disconnect(self, close_code):
        """Handle WebSocket disconnection."""
        # Leave all groups
        for group_name in self.groups:
            await self.channel_layer.group_discard(group_name, self.channel_name)
        
        logger.info(f"WebSocket disconnected: user={self.user_id}, code={close_code}")
    
    async def receive(self, text_data):
        """Handle incoming WebSocket messages."""
        try:
            data = json.loads(text_data)
            message_type = data.get('type')
            
            if message_type == 'hitl_response':
                await self._handle_hitl_response(data)
            elif message_type == 'subscribe':
                await self._handle_subscribe(data)
            elif message_type == 'unsubscribe':
                await self._handle_unsubscribe(data)
            elif message_type == 'ping':
                await self.send_json({'type': 'pong', 'timestamp': datetime.utcnow().isoformat()})
            else:
                await self.send_json({
                    'type': 'error',
                    'error': f'Unknown message type: {message_type}'
                })
                
        except json.JSONDecodeError:
            await self.send_json({
                'type': 'error',
                'error': 'Invalid JSON'
            })
        except Exception as e:
            logger.exception(f"Error processing WebSocket message: {e}")
            await self.send_json({
                'type': 'error',
                'error': 'Internal error processing message'
            })
    
    # Handler for execution events (from channel layer)
    async def execution_event(self, event):
        """Handle execution event from channel layer."""
        await self.send_json({
            'type': 'execution.event',
            'data': event.get('event', {})
        })
    
    # Handler for HITL requests (from channel layer)
    async def hitl_request(self, event):
        """Handle HITL request from channel layer."""
        await self.send_json({
            'type': 'hitl.request',
            'data': event.get('request', {})
        })
    
    # Handler for notifications (from channel layer)
    async def notification(self, event):
        """Handle notification from channel layer."""
        await self.send_json({
            'type': 'notification',
            'data': event.get('data', {})
        })
    
    async def _handle_hitl_response(self, data):
        """Process HITL response from client."""
        request_id = data.get('request_id')
        response = data.get('response')
        
        if not request_id or response is None:
            await self.send_json({
                'type': 'error',
                'error': 'Missing request_id or response'
            })
            return
        
        # Process the HITL response
        try:
            result = await self._save_hitl_response(request_id, response)
            
            await self.send_json({
                'type': 'hitl_response_ack',
                'data': {
                    'request_id': request_id,
                    'status': 'accepted' if result else 'error',
                }
            })
            
            # Notify executor to resume (if waiting)
            if result:
                await self._notify_execution_resume(request_id)
                
        except Exception as e:
            logger.exception(f"Error saving HITL response: {e}")
            await self.send_json({
                'type': 'error',
                'error': 'Failed to process HITL response'
            })
    
    async def _handle_subscribe(self, data):
        """Subscribe to additional execution."""
        execution_id = data.get('execution_id')
        
        if not execution_id:
            await self.send_json({
                'type': 'error',
                'error': 'Missing execution_id'
            })
            return
        
        # Verify access
        has_access = await self._verify_execution_access(execution_id)
        if not has_access:
            await self.send_json({
                'type': 'error',
                'error': 'Access denied to execution'
            })
            return
        
        # Join group
        group_name = f"execution_{execution_id}"
        if group_name not in self.groups:
            await self.channel_layer.group_add(group_name, self.channel_name)
            self.groups.append(group_name)
        
        await self.send_json({
            'type': 'subscribed',
            'data': {'execution_id': execution_id}
        })
    
    async def _handle_unsubscribe(self, data):
        """Unsubscribe from execution."""
        execution_id = data.get('execution_id')
        
        if not execution_id:
            return
        
        group_name = f"execution_{execution_id}"
        if group_name in self.groups:
            await self.channel_layer.group_discard(group_name, self.channel_name)
            self.groups.remove(group_name)
        
        await self.send_json({
            'type': 'unsubscribed',
            'data': {'execution_id': execution_id}
        })
    
    @database_sync_to_async
    def _verify_execution_access(self, execution_id: str) -> bool:
        """Verify user has access to execution."""
        from logs.models import ExecutionLog
        
        try:
            ExecutionLog.objects.get(
                execution_id=execution_id,
                user_id=self.user_id
            )
            return True
        except ExecutionLog.DoesNotExist:
            return False
    
    @database_sync_to_async
    def _save_hitl_response(self, request_id: str, response: dict) -> bool:
        """Save HITL response to database."""
        from orchestrator.models import HITLRequest
        
        try:
            hitl_request = HITLRequest.objects.get(
                request_id=request_id,
                user_id=self.user_id,
                status='pending'
            )
            
            # Determine status based on response
            response_value = response.get('value', response)
            if response_value in ('approve', 'approved', True):
                hitl_request.status = 'approved'
            elif response_value in ('reject', 'rejected', False):
                hitl_request.status = 'rejected'
            else:
                hitl_request.status = 'answered'
            
            hitl_request.response = response
            hitl_request.responded_at = timezone.now()
            hitl_request.save()
            
            return True
            
        except HITLRequest.DoesNotExist:
            logger.warning(f"HITL request not found or not pending: {request_id}")
            return False
    
    async def _notify_execution_resume(self, request_id: str):
        """Notify executor that HITL response is ready."""
        # Send to execution-specific channel
        hitl_request = await self._get_hitl_request(request_id)
        if hitl_request:
            execution_id = await self._get_execution_id(hitl_request)
            if execution_id:
                await self.channel_layer.group_send(
                    f"executor_{execution_id}",
                    {
                        'type': 'hitl.response_received',
                        'request_id': request_id,
                    }
                )
    
    @database_sync_to_async
    def _get_hitl_request(self, request_id: str):
        """Get HITL request from database."""
        from orchestrator.models import HITLRequest
        try:
            return HITLRequest.objects.get(request_id=request_id)
        except HITLRequest.DoesNotExist:
            return None
    
    @database_sync_to_async
    def _get_execution_id(self, hitl_request) -> Optional[str]:
        """Get execution ID from HITL request."""
        if hitl_request and hitl_request.execution:
            return str(hitl_request.execution.execution_id)
        return None
    
    async def send_json(self, data: dict):
        """Send JSON data to client."""
        await self.send(text_data=json.dumps(data))


class HITLNotificationConsumer(AsyncWebsocketConsumer):
    """
    Dedicated consumer for HITL notifications.
    
    Allows users to receive pending HITL requests across all executions.
    
    Route: /ws/hitl/<user_id>/
    """
    
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.user_id: Optional[int] = None
        self.group_name: Optional[str] = None
    
    async def connect(self):
        """Handle connection."""
        user = self.scope.get('user')
        if not user or not user.is_authenticated:
            await self.close(code=4001)
            return
        
        self.user_id = user.pk
        self.group_name = f"hitl_{self.user_id}"
        
        await self.channel_layer.group_add(self.group_name, self.channel_name)
        await self.accept()
        
        # Send pending HITL requests
        pending = await self._get_pending_requests()
        await self.send_json({
            'type': 'connected',
            'data': {
                'user_id': self.user_id,
                'pending_requests': pending,
            }
        })
    
    async def disconnect(self, close_code):
        """Handle disconnection."""
        if self.group_name:
            await self.channel_layer.group_discard(self.group_name, self.channel_name)
    
    async def receive(self, text_data):
        """Handle incoming messages."""
        try:
            data = json.loads(text_data)
            message_type = data.get('type')
            
            if message_type == 'respond':
                # Forward to ExecutionConsumer logic
                request_id = data.get('request_id')
                response = data.get('response')
                
                if request_id and response:
                    result = await self._save_hitl_response(request_id, response)
                    await self.send_json({
                        'type': 'response_ack',
                        'data': {
                            'request_id': request_id,
                            'success': result,
                        }
                    })
            elif message_type == 'refresh':
                pending = await self._get_pending_requests()
                await self.send_json({
                    'type': 'pending_requests',
                    'data': pending,
                })
                
        except json.JSONDecodeError:
            await self.send_json({'type': 'error', 'error': 'Invalid JSON'})
    
    async def hitl_new_request(self, event):
        """Handle new HITL request notification."""
        await self.send_json({
            'type': 'new_request',
            'data': event.get('request', {})
        })
    
    @database_sync_to_async
    def _get_pending_requests(self) -> list:
        """Get pending HITL requests for user."""
        from orchestrator.models import HITLRequest
        
        requests = HITLRequest.objects.filter(
            user_id=self.user_id,
            status='pending'
        ).order_by('-created_at')[:20]
        
        return [
            {
                'request_id': str(r.request_id),
                'type': r.request_type,
                'title': r.title,
                'message': r.message,
                'options': r.options,
                'created_at': r.created_at.isoformat(),
            }
            for r in requests
        ]
    
    @database_sync_to_async
    def _save_hitl_response(self, request_id: str, response: dict) -> bool:
        """Save HITL response."""
        from orchestrator.models import HITLRequest
        
        try:
            hitl_request = HITLRequest.objects.get(
                request_id=request_id,
                user_id=self.user_id,
                status='pending'
            )
            
            response_value = response.get('value', response)
            if response_value in ('approve', 'approved', True):
                hitl_request.status = 'approved'
            elif response_value in ('reject', 'rejected', False):
                hitl_request.status = 'rejected'
            else:
                hitl_request.status = 'answered'
            
            hitl_request.response = response
            hitl_request.responded_at = timezone.now()
            hitl_request.save()
            
            return True
        except HITLRequest.DoesNotExist:
            return False
    
    async def send_json(self, data: dict):
        """Send JSON data."""
        await self.send(text_data=json.dumps(data))


# Helper function to send HITL request to user via WebSocket
async def send_hitl_request_to_user(user_id: int, request_data: dict):
    """
    Send HITL request notification to user.
    
    Args:
        user_id: User to notify
        request_data: HITL request details
    """
    from channels.layers import get_channel_layer
    
    channel_layer = get_channel_layer()
    if channel_layer:
        await channel_layer.group_send(
            f"hitl_{user_id}",
            {
                'type': 'hitl.new_request',
                'request': request_data,
            }
        )


# Helper function to broadcast execution event
async def broadcast_execution_event(execution_id: str, event_data: dict):
    """
    Broadcast execution event to all subscribers.
    
    Args:
        execution_id: Execution UUID
        event_data: Event details
    """
    from channels.layers import get_channel_layer
    
    channel_layer = get_channel_layer()
    if channel_layer:
        await channel_layer.group_send(
            f"execution_{execution_id}",
            {
                'type': 'execution.event',
                'event': event_data,
            }
        )
