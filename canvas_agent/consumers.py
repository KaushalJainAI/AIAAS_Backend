import json
import logging
from channels.generic.websocket import AsyncWebsocketConsumer
from django.core.cache import cache

logger = logging.getLogger(__name__)


class CanvasAgentConsumer(AsyncWebsocketConsumer):
    async def connect(self):
        self.user = self.scope.get("user")
        self.user_id = None
        self.group_name = None

        if self.user and self.user.is_authenticated:
            self.user_id = self.user.id
            self.group_name = f"canvas_agent_{self.user_id}"

            await self.channel_layer.group_add(
                self.group_name,
                self.channel_name
            )
            await self.accept()
            logger.info(f"CanvasAgentConsumer connected for user {self.user_id}")
        else:
            await self.close()

    async def disconnect(self, close_code):
        """Handle disconnection gracefully — never block or raise."""
        try:
            if self.group_name:
                await self.channel_layer.group_discard(
                    self.group_name,
                    self.channel_name
                )
        except Exception as e:
            logger.warning(f"CanvasAgentConsumer disconnect cleanup error: {e}")
        finally:
            logger.info(
                f"CanvasAgentConsumer disconnected for user "
                f"{self.user_id or '?'} (code={close_code})"
            )

    async def receive(self, text_data):
        """
        Handle incoming messages from the frontend (e.g., canvas state updates).
        """
        try:
            data = json.loads(text_data)
            message_type = data.get('type')

            if message_type == 'canvas_state':
                state = data.get('state', {})
                # Use async cache to avoid blocking the event loop
                await cache.aset(
                    f"canvas_state_{self.user_id}", state, timeout=3600
                )

                await self.send(text_data=json.dumps({
                    'type': 'status',
                    'message': 'Canvas state cached'
                }))
        except Exception as e:
            logger.error(f"Error in CanvasAgentConsumer.receive: {e}")
            try:
                await self.send(text_data=json.dumps({
                    'type': 'error',
                    'message': str(e)
                }))
            except Exception:
                pass  # Connection may already be closed

    async def dispatch_actions(self, event):
        """
        Handle action dispatch events sent from the backend logic/views.
        This is a batch handler.
        """
        actions = event.get('actions', [])

        try:
            # Send action batch to the frontend
            await self.send(text_data=json.dumps({
                'type': 'canvas_action',
                'actions': actions
            }))
        except Exception as e:
            logger.warning(f"Failed to dispatch actions to client: {e}")
