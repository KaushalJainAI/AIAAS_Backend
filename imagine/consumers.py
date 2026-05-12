"""WebSocket consumer for the Imagine media agent.

Group: imagine_agent_{user_id}
Server -> client events (forwarded from dispatcher / hitl):
  - generation.started, generation.completed, generation.failed
  - imagine.hitl_request
"""
import json
import logging

from channels.generic.websocket import AsyncWebsocketConsumer

logger = logging.getLogger(__name__)


class ImagineAgentConsumer(AsyncWebsocketConsumer):
    async def connect(self):
        self.user = self.scope.get("user")
        self.user_id = None
        self.group_name = None

        if self.user and self.user.is_authenticated:
            self.user_id = self.user.id
            self.group_name = f"imagine_agent_{self.user_id}"
            await self.channel_layer.group_add(self.group_name, self.channel_name)
            await self.accept()
            await self.send(text_data=json.dumps({"type": "connected", "user_id": self.user_id}))
            logger.info(f"ImagineAgentConsumer connected for user {self.user_id}")
        else:
            await self.close(code=4001)

    async def disconnect(self, close_code):
        try:
            if self.group_name:
                await self.channel_layer.group_discard(self.group_name, self.channel_name)
        except Exception as e:
            logger.warning(f"ImagineAgentConsumer disconnect cleanup error: {e}")

    async def receive(self, text_data):
        try:
            data = json.loads(text_data)
        except Exception:
            return
        if data.get("type") == "ping":
            await self.send(text_data=json.dumps({"type": "pong"}))

    async def imagine_event(self, event):
        """Forward a group_send'd event to the client."""
        await self.send(text_data=json.dumps({
            "type": event.get("event", "imagine.event"),
            "data": event.get("data", {}),
        }))
