"""
Cost math for the model registry.

`AIModel`'s four rates are USD per 1M tokens as listed by the upstream provider
or OpenRouter. This module is the single place that turns a `TokenUsage` into
dollars, so the run detail, the agent list, the spend-cap guardrail and any
future invoice export agree by construction rather than by coincidence — the
divergence `agents/spend.py` exists to document is what happens when they do
not.

Three things this module insists on.

**A reported cost beats an estimate.** OpenRouter tells us what it actually
charged (`llm/handlers/llm_providers.py` asks for it); no price table we
maintain can be more accurate than the invoice, and ours goes stale the day a
model is repriced upstream.

**Free and unpriced are different answers.** A model with no row in the
registry costs an unknown amount, not zero. Returning `Decimal("0")` for both
is how a run on a model nobody priced shows up as free — so every entry point
returns a `(cost, source)` pair and `unpriced` is a first-class outcome that
the UI renders as "—" rather than as a number.

**Cache is three buckets, not a discount.** `TokenUsage` arrives already
normalised to disjoint buckets (see `llm/usage.py`), so pricing is a plain
weighted sum and no caller has to remember whether cached tokens were included
in the input count.
"""
from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP
from typing import Literal

from django.core.exceptions import ObjectDoesNotExist

from .usage import TokenUsage

# Keep USD math in Decimal to avoid float drift on invoices.
THOUSAND = Decimal("1000000")
USD_TO_INR = Decimal("88.0")  # conservative Aug 2026 rate; bump if you invoice in INR

#: Where a cost figure came from. `billed` is the provider's own number,
#: `estimated` is ours from the price table, `unpriced` means we do not know —
#: and a caller that renders `unpriced` as a number is lying.
CostSource = Literal["billed", "estimated", "unpriced"]

#: Providers whose models run on hardware the user already owns. A zero here is
#: a real zero, not a missing price.
LOCAL_PROVIDERS = frozenset({"ollama"})

#: Money is stored and compared at six decimal places: a single cheap turn can
#: cost $0.0003, and rounding that to four would floor a whole run of them to
#: zero — the same "small things are free" failure `agents/spend.py::rupees_for`
#: rounds *up* to avoid.
_CENTS = Decimal("0.000001")


def _q(value: Decimal) -> Decimal:
    return value.quantize(_CENTS, rounding=ROUND_HALF_UP)


def _rate(value) -> Decimal:
    return Decimal(str(value)) if value is not None else Decimal("0")


def estimate_cost_usd(
    *,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cached_read_tokens: int = 0,
    cached_write_tokens: int = 0,
    input_price_per_million=Decimal("0"),
    output_price_per_million=Decimal("0"),
    cached_input_price_per_million=None,
    cache_write_price_per_million=None,
) -> Decimal:
    """USD for one model call, from four disjoint token buckets.

    `input_tokens` must ALREADY exclude the cached ones — pass a normalised
    `TokenUsage`, or subtract yourself. This function cannot detect the
    mistake: an inclusive count simply prices the cached prefix twice and
    returns a plausible number. `cost_for_usage` is the entry point that
    cannot be called wrongly, and should be preferred.

    A missing cache-read rate falls back to the full input rate (the provider
    offers no discount); a missing cache-write rate is zero (the provider does
    not charge to write). The asymmetry is deliberate and matches what the
    providers actually do.
    """
    inp = _rate(input_price_per_million)
    out = _rate(output_price_per_million)
    read = (
        Decimal(str(cached_input_price_per_million))
        if cached_input_price_per_million is not None else inp
    )
    write = _rate(cache_write_price_per_million)

    cost = (
        Decimal(max(0, int(input_tokens or 0))) * inp
        + Decimal(max(0, int(cached_read_tokens or 0))) * read
        + Decimal(max(0, int(cached_write_tokens or 0))) * write
        + Decimal(max(0, int(output_tokens or 0))) * out
    ) / THOUSAND
    return _q(cost)


def cost_for_usage(
    model_value: str, usage: TokenUsage,
) -> tuple[Decimal, CostSource]:
    """What one model call cost, and how confident we are in that number.

    Resolution order:

    1. the provider's own reported charge, when it sent one (`billed`);
    2. our price table, when the model has a row with a rate on it
       (`estimated`);
    3. `unpriced` — a local/free model reports an honest `estimated` zero, but
       an unknown model, or a router row whose price varies by whatever it
       routes to, reports that we do not know.

    Never raises. A cost is telemetry about a call that already succeeded, and
    a registry lookup failing must not turn a finished turn into a failed one.
    """
    if usage is not None and usage.reported_cost_usd is not None:
        return _q(usage.reported_cost_usd), "billed"

    if usage is None or usage.is_empty:
        # Nothing was reported, so there is nothing to price. Distinguished
        # from a genuine zero for the same reason as an unknown model.
        return Decimal("0"), "unpriced"

    from .models import AIModel

    try:
        # Deliberately NOT filtered on `is_active`. That flag decides whether a
        # model may be *offered*, which is a question about the future; this is
        # a question about the past. Models are retired constantly here, and a
        # run that happened on one still cost real money at a rate we still
        # have on file — reporting it `unpriced` would throw away a number we
        # know, and would make an agent's spend history quietly shrink every
        # time a model was deactivated.
        model = (
            AIModel.objects
            .select_related("provider")
            .get(value=model_value)
        )
    except (ObjectDoesNotExist, AIModel.MultipleObjectsReturned):
        return Decimal("0"), "unpriced"
    except Exception:  # noqa: BLE001 - a broken read must not fail the turn
        return Decimal("0"), "unpriced"

    provider_slug = (getattr(model.provider, "slug", "") or "").lower()
    priced = bool(model.input_price_per_million or model.output_price_per_million)
    if not priced:
        # A zero-rate row is either a genuinely free model (local weights, or a
        # provider's free tier) or a row whose price nobody filled in — most
        # often a router, where the rate depends on what it routes to. The
        # first is a real zero; the second is a hole, and saying so is the
        # whole point of `unpriced`.
        if model.is_free or provider_slug in LOCAL_PROVIDERS:
            return Decimal("0"), "estimated"
        return Decimal("0"), "unpriced"

    return estimate_cost_usd(
        input_tokens=usage.input,
        output_tokens=usage.output,
        cached_read_tokens=usage.cached_read,
        cached_write_tokens=usage.cached_write,
        input_price_per_million=model.input_price_per_million,
        output_price_per_million=model.output_price_per_million,
        cached_input_price_per_million=model.cached_input_price_per_million,
        cache_write_price_per_million=model.cache_write_price_per_million,
    ), "estimated"


def estimate_cost_for_model(
    model_value: str,
    *,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cached_input_tokens: int = 0,
) -> Decimal:
    """Cost one call by model id. Returns 0 for unknown/local models.

    Kept for callers that hold loose token counts rather than a `TokenUsage`.
    `cost_for_usage` is the better door: it reports *why* a cost is zero, which
    this signature has no way to express.
    """
    usage = TokenUsage(
        input=max(0, int(input_tokens or 0) - int(cached_input_tokens or 0)),
        output=int(output_tokens or 0),
        cached_read=int(cached_input_tokens or 0),
    )
    cost, _source = cost_for_usage(model_value, usage)
    return cost


def combine_sources(sources) -> CostSource:
    """The honest source for a total assembled from several calls.

    A run is priced turn by turn, and the turns need not agree: one may be
    billed by the provider and the next estimated. The total is only as good as
    its weakest part, so any `unpriced` turn makes the whole total `unpriced` —
    reporting a confident sum that silently omits a turn is worse than
    admitting the gap. `billed` survives only when every part was billed.
    """
    seen = {s for s in sources if s}
    if not seen:
        return "unpriced"
    if "unpriced" in seen:
        return "unpriced"
    if seen == {"billed"}:
        return "billed"
    return "estimated"


def quantize_usd(value) -> Decimal:
    """A money value at the six decimal places everything else here uses.

    Needed because a database `Sum` over a DecimalField does not preserve its
    scale — SQLite returns `0.00420000000000000` for a column declared at six
    places — and a cost rendered with fifteen digits of trailing noise reads as
    a bug in the number rather than in its formatting.
    """
    if value is None:
        return Decimal("0.000000")
    try:
        return _q(Decimal(str(value)))
    except (ArithmeticError, TypeError, ValueError):
        return Decimal("0.000000")


def format_usd(value) -> str:
    """A money value as the string that goes on the wire.

    A string rather than a float, deliberately: JSON has no decimal type, and a
    cost that acquires binary drift on the way to the browser is a cost that
    will not add up against the one the guardrail enforced.
    """
    return str(quantize_usd(value))


def usd_to_inr(usd) -> Decimal:
    return _q(Decimal(str(usd)) * USD_TO_INR)
