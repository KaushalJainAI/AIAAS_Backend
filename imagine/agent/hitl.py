"""HITL gate logic + WS notification."""
import logging
from typing import Any, Dict

logger = logging.getLogger(__name__)


def needs_hitl(intent: Dict[str, Any], cost_threshold: float) -> bool:
    if intent.get("clarifying_question"):
        return True
    if intent.get("missing_required"):
        return True
    if (intent.get("confidence") or 0) < 0.7:
        return True
    if intent.get("type") == "video":
        return True
    try:
        if float(intent.get("estimated_cost_usd") or 0) > cost_threshold:
            return True
    except (TypeError, ValueError):
        pass
    return False


def broadcast_hitl_request(conversation, intent: Dict[str, Any]) -> None:
    try:
        from channels.layers import get_channel_layer
        from asgiref.sync import async_to_sync
        layer = get_channel_layer()
        if not layer:
            return
        async_to_sync(layer.group_send)(
            f"imagine_agent_{conversation.user_id}",
            {
                "type": "imagine.event",
                "event": "imagine.hitl_request",
                "data": {
                    "conversation_id": conversation.id,
                    "intent": intent,
                },
            },
        )
    except Exception as e:
        logger.debug(f"HITL WS broadcast skipped: {e}")
