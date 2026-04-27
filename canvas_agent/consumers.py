import json
import logging
from channels.generic.websocket import AsyncWebsocketConsumer
from django.core.cache import cache

logger = logging.getLogger(__name__)

class CanvasAgentConsumer(AsyncWebsocketConsumer):
    async def connect(self):
        self.user = self.scope.get("user")
        if self.user and self.user.is_authenticated:
            self.group_name = f"canvas_agent_{self.user.id}"
            
            await self.channel_layer.group_add(
                self.group_name,
                self.channel_name
            )
            await self.accept()
            logger.info(f"CanvasAgentConsumer connected for user {self.user.id}")
        else:
            await self.close()

    async def disconnect(self, close_code):
        if hasattr(self, 'group_name'):
            await self.channel_layer.group_discard(
                self.group_name,
                self.channel_name
            )
            logger.info(f"CanvasAgentConsumer disconnected for user {self.user.id}")

    async def receive(self, text_data):
        """
        Handle incoming messages from the frontend (e.g., canvas state updates).
        """
        try:
            data = json.loads(text_data)
            message_type = data.get('type')
            
            if message_type == 'canvas_state':
                state = data.get('state', {})
                # Cache the state for the LLM to access
                cache.set(f"canvas_state_{self.user.id}", state, timeout=3600)
                
                await self.send(text_data=json.dumps({
                    'type': 'status',
                    'message': 'Canvas state cached'
                }))
        except Exception as e:
            logger.error(f"Error in CanvasAgentConsumer.receive: {e}")
            await self.send(text_data=json.dumps({
                'type': 'error',
                'message': str(e)
            }))

    async def dispatch_actions(self, event):
        """
        Handle action dispatch events sent from the backend logic/views.
        This is a batch handler.
        """
        actions = event.get('actions', [])
        
        # Send action batch to the frontend
        await self.send(text_data=json.dumps({
            'type': 'canvas_action',
            'actions': actions
        }))
