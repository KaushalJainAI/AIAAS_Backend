"""WebSocket consumer for the Imagine media agent.

Group: imagine_agent_{user_id}
Server -> client events (forwarded from dispatcher / hitl):
  - generation.started, generation.completed, generation.failed
  - imagine.hitl_request
"""
from core.realtime.consumers import UserGroupConsumer


class ImagineAgentConsumer(UserGroupConsumer):
    group_prefix = "imagine_agent"
    #: The media UI waits for this before enabling the generate button.
    send_connect_ack = True

    async def imagine_event(self, event):
        """Forward a group_send'd event to the client."""
        await self.send_json({
            "type": event.get("event", "imagine.event"),
            "data": event.get("data", {}),
        })
