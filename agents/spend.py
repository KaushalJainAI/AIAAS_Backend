"""
What an agent has spent, in the unit its cap is written in.

One conversion, in one place, read by both the guardrail that refuses a run
(`agent/runtime.py::check_guardrails`) and the number the agent list shows
(`views/agents.py::_with_stats`). That is the whole point of the module: a cap
enforced against one number while the UI displays another is a cap the user
cannot reason about, and before this the two disagreed completely — the UI
summed `credits_used` and so did the guardrail, and nothing writes that column,
so both reported zero for ever.

There are now two ways to price a run, and this module is where they meet.

**Accurate, when the run recorded a cost.** Since runs began recording their
token breakdown (`llm/usage.py`) and pricing each turn against the model it
actually ran on (`llm/pricing.py`), `ExecutionLog.cost_usd` is a real figure —
OpenRouter's own charge where it reported one, our price table otherwise.

**Blended, when it did not.** Every run predating that, and every run on a
model with no price on record, has only `tokens_used`. Those are costed at one
flat rate. It is deliberately not a per-model table: this is a blast-radius
control, not billing, and a second price table that must be kept current would
be neither right nor maintained.

The mix matters more than either half. A cap must never *stop* working because
a model is missing from the registry, so an unpriced run falls back rather than
counting as free — that is the whole reason the fallback survives at all.
"""
from __future__ import annotations

from decimal import ROUND_CEILING, Decimal

from workflow_backend.thresholds import RUPEES_PER_MILLION_TOKENS

#: Cost sources we trust enough to bill from. `unpriced` is deliberately absent:
#: it means "we do not know", and a run we cannot price must be charged at the
#: blended rate rather than at zero.
PRICED_SOURCES = ('billed', 'estimated')


def rupees_for(tokens: int | None) -> int:
    """Approximate rupee cost of `tokens` at the blended rate, rounded up.

    Rounded *up* so a run is never free: a floor would let an unbounded number
    of small runs sit at zero and never approach the cap, which is the failure
    this whole path exists to prevent.
    """
    tokens = int(tokens or 0)
    if tokens <= 0:
        return 0
    return -(-tokens * RUPEES_PER_MILLION_TOKENS // 1_000_000)


def rupees_for_usd(usd: Decimal | float | str | None) -> int:
    """Rupee cost of a recorded USD figure, rounded up for the same reason."""
    from llm.pricing import usd_to_inr

    if usd is None:
        return 0
    inr = usd_to_inr(usd)
    if inr <= 0:
        return 0
    return int(inr.to_integral_value(rounding=ROUND_CEILING))


def spend_rupees(*, cost_usd, cost_source: str, tokens: int | None) -> int:
    """One run's spend, preferring what it recorded over what we can guess.

    The fallback is the point: a model missing from the price registry must not
    make its runs free, or the cap silently stops applying to exactly the
    models nobody has got around to pricing.
    """
    if cost_source in PRICED_SOURCES and cost_usd:
        return rupees_for_usd(cost_usd)
    return rupees_for(tokens)


def aggregate_rupees(queryset) -> int:
    """Total rupee spend across an `ExecutionLog` queryset.

    Two sums rather than a row-by-row loop, because both callers are on a hot
    path (a guardrail before every run, and a list endpoint). Priced runs are
    summed in USD and converted once; the rest fall back to the blended rate.

    `cost_source` is a plain CharField, so `exclude` is safe here — unlike the
    JSON-key filter in `notifications/reminders.py`, where `NOT (key = False)`
    is NULL for a missing key and silences the rows it should match.
    """
    from django.db.models import Q, Sum

    totals = queryset.aggregate(
        priced_usd=Sum('cost_usd', filter=Q(cost_source__in=PRICED_SOURCES)),
        unpriced_tokens=Sum(
            'tokens_used', filter=~Q(cost_source__in=PRICED_SOURCES)
        ),
    )
    return (
        rupees_for_usd(totals['priced_usd'])
        + rupees_for(totals['unpriced_tokens'])
    )
