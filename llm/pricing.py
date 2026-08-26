"""
Cost math for the model registry.

`AIModel.input_price_per_million` / `output_price_per_million` are USD per
1M tokens as listed by the upstream provider or OpenRouter on 2026-08-24.
This module is the single place that turns token counts into dollars so the
UI, the spend-cap guardrail, and any future invoice export agree.

Spend-cap (`agents/spend.py::rupees_for`) stays as a blunt blast-radius
control in INR. This is accurate billing in USD.
"""
from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP

from django.core.exceptions import ObjectDoesNotExist

# Keep USD math in Decimal to avoid float drift on invoices.
THOUSAND = Decimal("1000000")
USD_TO_INR = Decimal("88.0")  # conservative Aug 2026 rate; bump if you invoice in INR


def _q4(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)


def estimate_cost_usd(
    *,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cached_input_tokens: int = 0,
    input_price_per_million: Decimal | float | str = Decimal("0"),
    output_price_per_million: Decimal | float | str = Decimal("0"),
    cached_input_price_per_million: Decimal | float | str | None = None,
) -> Decimal:
    """
    USD cost for a single model call.

    Cached tokens are billed at `cached_input_price_per_million` when given,
    otherwise at the regular input rate. Pass `cached_input_tokens=0` when the
    provider didn't report a cache split — the whole input is then costed at
    `input_price_per_million`.
    """
    inp = Decimal(str(input_price_per_million))
    out = Decimal(str(output_price_per_million))
    cache = Decimal(str(cached_input_price_per_million)) if cached_input_price_per_million is not None else inp

    # `cached_input_tokens` are a subset of `input_tokens` on providers that
    # report it (Anthropic, OpenAI). Callers that have the split should pass
    #   input_tokens = total_input
    #   cached_input_tokens = cache_hit_tokens
    # and we bill: (total - cached) * inp + cached * cache
    cached = int(cached_input_tokens or 0)
    total_in = int(input_tokens or 0)
    uncached = max(0, total_in - cached)

    cost = (Decimal(uncached) * inp / THOUSAND) + (Decimal(cached) * cache / THOUSAND) + (Decimal(int(output_tokens or 0)) * out / THOUSAND)
    return _q4(cost)


def estimate_cost_for_model(
    model_value: str,
    *,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cached_input_tokens: int = 0,
) -> Decimal:
    """Lookup `AIModel` by `value` and cost it. Returns 0 for unknown/local models."""
    from .models import AIModel

    try:
        m = AIModel.objects.get(value=model_value, is_active=True)
    except ObjectDoesNotExist:
        # Ollama local models and retired ids are costed as 0 — nothing to bill.
        return Decimal("0.0000")
    return estimate_cost_usd(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cached_input_tokens=cached_input_tokens,
        input_price_per_million=m.input_price_per_million,
        output_price_per_million=m.output_price_per_million,
        cached_input_price_per_million=m.cached_input_price_per_million,
    )


def usd_to_inr(usd: Decimal | float | str) -> Decimal:
    return _q4(Decimal(str(usd)) * USD_TO_INR)
