import json
import logging
from channels.generic.websocket import AsyncWebsocketConsumer

logger = logging.getLogger(__name__)


class BuddyConsumer(AsyncWebsocketConsumer):
    async def connect(self):
        # We can use the user id as a group name to allow targeted control
        self.user = self.scope.get("user")
        self.user_id = None
        self.group_name = None

        if self.user and self.user.is_authenticated:
            self.user_id = self.user.id
            self.group_name = f"buddy_{self.user_id}"

            # Join group
            await self.channel_layer.group_add(
                self.group_name,
                self.channel_name
            )
            await self.accept()
        else:
            await self.close()

    async def disconnect(self, close_code):
        """Handle disconnection gracefully — never block or raise."""
        try:
            if self.group_name:
                # Leave group
                await self.channel_layer.group_discard(
                    self.group_name,
                    self.channel_name
                )
        except Exception as e:
            logger.warning(f"BuddyConsumer disconnect cleanup error: {e}")
        finally:
            logger.info(
                f"BuddyConsumer disconnected for user "
                f"{self.user_id or '?'} (code={close_code})"
            )

    async def receive(self, text_data):
        """
        Handle incoming messages from the frontend (e.g., context updates).
        """
        try:
            data = json.loads(text_data)
            message_type = data.get('type')

            if message_type == 'context_update':
                # Handle context update (e.g., screen content)
                context = data.get('context', {})

                # Use async cache to avoid blocking the event loop
                from django.core.cache import cache
                await cache.aset(
                    f"buddy_context_{self.user_id}", context, timeout=3600
                )

                # Echo for now
                await self.send(text_data=json.dumps({
                    'type': 'status',
                    'message': 'Context updated'
                }))

            elif message_type == 'request_action':
                # Handle a request for an action (if the frontend asks the assistant for help)
                # This is where we'd call the AI logic
                pass
        except Exception as e:
            logger.error(f"Error in BuddyConsumer.receive: {e}")
            try:
                await self.send(text_data=json.dumps({
                    'type': 'error',
                    'message': str(e)
                }))
            except Exception:
                pass  # Connection may already be closed

    async def trigger_action(self, event):
        """
        Handle action trigger events sent from the backend logic/views.
        """
        action = event['action']
        params = event.get('parameters', {})

        try:
            # Send action to the frontend
            await self.send(text_data=json.dumps({
                'type': 'trigger_action',
                'action': action,
                'parameters': params
            }))
        except Exception as e:
            logger.warning(f"Failed to send trigger_action to client: {e}")
