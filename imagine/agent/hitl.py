"""HITL gate logic. The WS notification lives in `services/events.py`."""
from typing import Any, Dict

from ..services.events import broadcast_hitl_request

#: The router can under-report cost — including at the user's prompting, since
#: the request text shares the classifier's context — so the gate never treats
#: a generation as cheaper than its modality's floor.
COST_FLOORS: Dict[str, float] = {
    "image": 0.02,
    "audio": 0.015,
    "video": 0.30,
}


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
        estimated = float(intent.get("estimated_cost_usd") or 0)
    except (TypeError, ValueError):
        estimated = 0.0
    kind = intent.get("type")
    if max(estimated, COST_FLOORS.get(kind, 0.0)) > cost_threshold:
        return True
    return False
