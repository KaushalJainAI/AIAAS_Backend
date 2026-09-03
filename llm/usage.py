"""
What a model call actually consumed, in the shape cost can be computed from.

One reader for every provider's `usage` object, because the providers disagree
about the one thing that matters most — **whether the reported input count
already includes the tokens served from cache**:

    inclusive  (OpenAI, and everything OpenAI-compatible)
        prompt_tokens INCLUDES prompt_tokens_details.cached_tokens.
        Cached tokens are a *subset* to be subtracted before pricing the rest
        at the full input rate.

    exclusive  (Anthropic's native API)
        input_tokens EXCLUDES cache_read_input_tokens and
        cache_creation_input_tokens. The three are *addends*.

Bill an inclusive payload with exclusive math and every cached token is counted
twice; do the reverse and the whole cached prefix is billed as free. Neither
mistake shows up as an error — only as a number that is quietly wrong — so the
convention is declared per handler (`OpenAICompatibleLLMNode.usage_convention`)
and normalised here rather than guessed from the keys present.

All four providers this platform supports (OpenRouter, OpenAI, NVIDIA NIM,
Ollama) speak the OpenAI protocol, so they are all `inclusive`. `exclusive`
exists because OpenRouter proxies Anthropic models, and the day someone adds a
native Anthropic handler the convention must be a field they set rather than an
assumption baked into the arithmetic.

`normalize()` never raises: usage is telemetry riding on a successful call, and
a malformed usage object must not fail a turn that already has its answer.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

Convention = Literal["inclusive", "exclusive"]

#: What `normalize` assumes when a handler declares nothing. The OpenAI
#: protocol is the one every supported provider speaks.
DEFAULT_CONVENTION: Convention = "inclusive"


def _int(value: Any) -> int:
    """A non-negative int from whatever the provider sent, else 0."""
    try:
        out = int(value)
    except (TypeError, ValueError):
        return 0
    return out if out > 0 else 0


def _decimal(value: Any) -> Decimal | None:
    """A Decimal from a provider-reported cost, or None if it wasn't one.

    Goes through `str` deliberately: providers send costs as JSON floats, and
    `Decimal(0.0000123)` carries the float's binary error into money.
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        out = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return out if out >= 0 else None


@dataclass(frozen=True, slots=True)
class TokenUsage:
    """One model call's consumption, always in *exclusive* terms.

    Whatever the provider's convention, the fields here are disjoint buckets:
    `input` is the part that was neither read from nor written to cache, so
    `input + cached_read + cached_write` is the whole prompt and the three can
    be priced at three rates and added. Normalising to one convention at the
    edge is what lets `pricing.cost_for_usage` have a single code path.
    """

    #: Prompt tokens billed at the full input rate — cache excluded.
    input: int = 0
    #: Completion tokens. Includes reasoning tokens, which providers bill as
    #: output; `reasoning` below is a *subset* kept for display only.
    output: int = 0
    #: Prompt tokens served from cache, billed at the (much lower) read rate.
    cached_read: int = 0
    #: Prompt tokens written into the cache. Free on OpenAI, ~1.25x on
    #: Anthropic, which is why it is priced separately rather than folded in.
    cached_write: int = 0
    #: Reasoning tokens, a subset of `output`. Never added to a cost — doing so
    #: would double-bill it — but worth showing, since a run whose spend is all
    #: reasoning is a different problem from one whose spend is all answer.
    reasoning: int = 0
    #: The provider's own total, when it sent one. Kept as reported rather than
    #: recomputed: a mismatch with `input + output + cache` is a signal that
    #: this normaliser has met a shape it does not know.
    total: int = 0
    #: What the provider says it actually charged, in USD. OpenRouter returns
    #: this when the request asks for it. Authoritative over any estimate we
    #: could make, because it is the number that will appear on the invoice.
    reported_cost_usd: Decimal | None = None

    @property
    def prompt(self) -> int:
        """Every prompt token, however it was billed."""
        return self.input + self.cached_read + self.cached_write

    @property
    def billable_total(self) -> int:
        """Prompt + completion. What `total` should equal if we read it right."""
        return self.prompt + self.output

    @property
    def is_empty(self) -> bool:
        """True when the provider reported nothing at all.

        A call that genuinely consumed nothing does not exist, so this means
        *not reported* — which a caller must be able to tell apart from zero,
        or a provider that stopped sending usage looks like a free run.
        """
        return self.billable_total == 0 and self.total == 0

    def __add__(self, other: TokenUsage) -> TokenUsage:
        """Fold two calls together. Used to accumulate a stream, and a run.

        `reported_cost_usd` adds only when at least one side reported one;
        summing a reported cost with a missing one as if it were zero would
        turn a partially-reported run into a confidently understated bill.
        """
        if not isinstance(other, TokenUsage):  # pragma: no cover - defensive
            return NotImplemented
        costs = [c for c in (self.reported_cost_usd, other.reported_cost_usd)
                 if c is not None]
        return TokenUsage(
            input=self.input + other.input,
            output=self.output + other.output,
            cached_read=self.cached_read + other.cached_read,
            cached_write=self.cached_write + other.cached_write,
            reasoning=self.reasoning + other.reasoning,
            total=self.total + other.total,
            reported_cost_usd=sum(costs, Decimal("0")) if costs else None,
        )

    __radd__ = __add__

    def as_dict(self) -> dict[str, Any]:
        """Plain JSON-able shape, for a wire payload or a log line."""
        return {
            "input": self.input,
            "output": self.output,
            "cached_read": self.cached_read,
            "cached_write": self.cached_write,
            "reasoning": self.reasoning,
            "total": self.total,
            "reported_cost_usd": (
                str(self.reported_cost_usd)
                if self.reported_cost_usd is not None else None
            ),
        }


EMPTY_USAGE = TokenUsage()


def normalize(raw: Any, convention: Convention = DEFAULT_CONVENTION) -> TokenUsage:
    """One provider `usage` object as a `TokenUsage`.

    Reads every spelling the supported providers use:

    * `prompt_tokens` / `completion_tokens` / `total_tokens` — OpenAI protocol
    * `prompt_tokens_details.cached_tokens` — OpenAI's cache hit count
    * `completion_tokens_details.reasoning_tokens` — reasoning subset
    * `input_tokens` / `output_tokens` and `cache_read_input_tokens` /
      `cache_creation_input_tokens` — Anthropic's native spelling
    * `cost` — OpenRouter's actual charge, when the request asked for it

    Anything unreadable degrades to zero for that field rather than raising.
    """
    if not isinstance(raw, dict) or not raw:
        return EMPTY_USAGE

    prompt = _int(raw.get("prompt_tokens")) or _int(raw.get("input_tokens"))
    output = _int(raw.get("completion_tokens")) or _int(raw.get("output_tokens"))

    details = raw.get("prompt_tokens_details")
    cached_read = _int(details.get("cached_tokens")) if isinstance(details, dict) else 0
    # Anthropic's spelling, and what OpenRouter forwards for Anthropic models.
    cached_read = cached_read or _int(raw.get("cache_read_input_tokens"))
    cached_write = _int(raw.get("cache_creation_input_tokens"))

    out_details = raw.get("completion_tokens_details")
    reasoning = (
        _int(out_details.get("reasoning_tokens"))
        if isinstance(out_details, dict) else 0
    )
    reasoning = reasoning or _int(raw.get("reasoning_tokens"))

    if convention == "inclusive":
        # `prompt` counts the cached tokens too. Subtract them so the three
        # buckets are disjoint — and clamp at zero, because a provider whose
        # details disagree with its own total must not produce a negative
        # count that then prices as a credit.
        uncached = max(0, prompt - cached_read - cached_write)
    else:
        uncached = prompt

    total = _int(raw.get("total_tokens"))
    usage = TokenUsage(
        input=uncached,
        output=output,
        cached_read=cached_read,
        cached_write=cached_write,
        reasoning=reasoning,
        total=total,
        reported_cost_usd=_decimal(raw.get("cost")),
    )
    # Providers that omit `total_tokens` are common enough that deriving it
    # here keeps every downstream reader from having to. Derived, never
    # overwritten: a reported total is evidence about how we read the rest.
    if not usage.total:
        usage = replace(usage, total=usage.billable_total)
    return usage
