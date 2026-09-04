"""One place that pushes Imagine events over the per-user WS group.

The dispatcher, the HITL gate and the video poller each carried their own
`group_send` helper with a slightly different payload and the group-name
string hardcoded three times — the drift pattern the rest of the platform
centralises. The group name, the frame shape and the "Channels may be
unconfigured" no-op now live here.

`get_channel_layer()` returning None is the one silent path: that means
Channels is not installed, which is a legitimate local-dev configuration.
Every other failure is logged at warning level instead of being swallowed at
debug, so a dead channel layer stops hiding from the operator.
"""
import logging

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer

logger = logging.getLogger(__name__)


def _send(user_id: int, event: str, data: dict) -> None:
    try:
        layer = get_channel_layer()
        if layer is None:
            return
        async_to_sync(layer.group_send)(
            f"imagine_agent_{user_id}",
            {"type": "imagine.event", "event": event, "data": data},
        )
    except Exception:
        logger.warning(
            "Imagine WS broadcast failed (event=%s, user=%s)",
            event, user_id, exc_info=True,
        )


def broadcast_generation(generation, event: str, **extra) -> None:
    """Push a generation lifecycle event (started/completed/failed/progress)."""
    _send(
        generation.user_id,
        event,
        {
            "generation_id": generation.id,
            "status": generation.status,
            "type": generation.type,
            "output_url": generation.output_url,
            "error": generation.error_message,
            **extra,
        },
    )


def broadcast_hitl_request(conversation, intent: dict) -> None:
    """Ask the user to approve/edit/cancel a pending generation plan."""
    _send(
        conversation.user_id,
        "imagine.hitl_request",
        {"conversation_id": conversation.id, "intent": intent},
    )