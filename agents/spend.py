"""
What an agent has spent, in the unit its cap is written in.

One conversion, in one place, read by both the guardrail that refuses a run
(`agent/runtime.py::check_guardrails`) and the number the agent list shows
(`views/agents.py::_with_stats`). That is the whole point of the module: a cap
enforced against one number while the UI displays another is a cap the user
cannot reason about, and before this the two disagreed completely — the UI
summed `credits_used` and so did the guardrail, and nothing writes that column,
so both reported zero for ever.
"""
from __future__ import annotations

from workflow_backend.thresholds import RUPEES_PER_MILLION_TOKENS


def rupees_for(tokens: int | None) -> int:
    """Approximate rupee cost of `tokens`, rounded up.

    Rounded *up* so a run is never free: a floor would let an unbounded number
    of small runs sit at zero and never approach the cap, which is the failure
    this whole path exists to prevent.
    """
    tokens = int(tokens or 0)
    if tokens <= 0:
        return 0
    return -(-tokens * RUPEES_PER_MILLION_TOKENS // 1_000_000)
