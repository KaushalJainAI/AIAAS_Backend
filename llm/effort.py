"""
Effort levels — how hard a model is asked to think before it answers.

Every current reasoning model exposes some version of this knob, and every
provider spells it differently: OpenAI sends `reasoning_effort`, OpenRouter
wraps it as `reasoning: {"effort": ...}`, Ollama calls it `think`. The spelling
belongs to the handler; what belongs *here* is the vocabulary all three are
translated from, and the one rule that keeps a wrong level from turning into a
400 at the wire.

Two things are stored on `AIModel`, and they answer different questions.
`effort_levels` is **which rungs this model actually offers** — an empty list
means the model has no effort control at all, which is not the same as "offers
the default one". `default_effort` is the rung it runs at when nobody chooses,
blank meaning "let the provider decide", which is the only honest answer for a
model whose own default we have not measured.

The resolution rule is **snap, never refuse**. A user picks an effort, then
picks a different model in the same conversation; the level they chose may not
exist on the new one. Refusing there would fail a turn over a preference, so
`resolve` moves the request to the nearest rung the model does offer, and a
model offering nothing gets `None` — no field on the wire, which is exactly
what a non-reasoning model needs, because sending it one is a hard 400 on
OpenAI and a silent ignore everywhere else.
"""
from __future__ import annotations

import logging
import time
from typing import Iterable, Sequence

logger = logging.getLogger(__name__)

#: The canonical ladder, cheapest first. Every provider's own vocabulary maps
#: onto these five; a name outside them is not a level, it is a typo.
#:
#: `none` is a real rung rather than the absence of one: a reasoning model that
#: *can* be told not to think is a different thing from a model that cannot
#: think at all, and only the first can be asked for a fast answer. It is the
#: rung the handlers work hardest to encode — OpenRouter takes
#: `reasoning: {"enabled": false}`, Ollama takes `think: false`, and OpenAI
#: takes nothing at all, so `none` there degrades to `minimal`.
LADDER: tuple[str, ...] = ("none", "minimal", "low", "medium", "high")

_RANK = {name: index for index, name in enumerate(LADDER)}

#: The three rungs nearly every reasoning model serves. Named because it is
#: what most catalogue rows declare, and a shared constant is one place to fix
#: when a provider adds a fourth.
STANDARD: tuple[str, ...] = ("low", "medium", "high")

#: OpenAI's GPT-5 tiers, which add a cheaper rung below `low`.
WITH_MINIMAL: tuple[str, ...] = ("minimal", "low", "medium", "high")

#: Models whose reasoning can be switched off entirely — the hybrid ones that
#: serve both a thinking and a non-thinking mode from the same checkpoint.
TOGGLEABLE: tuple[str, ...] = ("none", "low", "medium", "high")

#: Everything, for a model that serves the whole ladder.
ALL: tuple[str, ...] = LADDER


def normalize(value: object) -> str | None:
    """A level name, or None if `value` is not one.

    Deliberately lenient about case and whitespace and strict about everything
    else: this reads request bodies and database rows, and both can carry
    `"High"` or `" high "` without meaning anything different.
    """
    if not isinstance(value, str):
        return None
    name = value.strip().lower()
    return name if name in _RANK else None


def clean_levels(values: object) -> tuple[str, ...]:
    """Normalise a declared `effort_levels` list into ladder order.

    Ladder order rather than the order it was written in, because `nearest`
    walks outward from a rank and every caller that renders a picker wants the
    rungs cheapest-first. Unknown names are dropped rather than raising: this
    also reads admin-edited rows, and one bad string should cost that model its
    effort control, not the whole catalogue read.
    """
    if not isinstance(values, (list, tuple)):
        return ()
    seen = {name for value in values if (name := normalize(value))}
    return tuple(level for level in LADDER if level in seen)


def nearest(requested: str, offered: Sequence[str]) -> str | None:
    """The offered rung closest to `requested` on the ladder.

    Ties break *downward* — asked for something between `low` and `high` on a
    model offering only those two, the cheaper one wins. A user who wanted the
    expensive answer can ask for it by name; nobody wants a surprise bill from
    a tie-break.
    """
    if not offered:
        return None
    if requested in offered:
        return requested
    target = _RANK[requested]
    return min(offered, key=lambda level: (abs(_RANK[level] - target), _RANK[level]))


def resolve(
    requested: object,
    *,
    offered: Iterable[str],
    default: object = "",
) -> str | None:
    """The level to actually send, given what the caller asked for.

    `None` means send nothing — either the model has no effort control, or
    neither the caller nor the catalogue expressed a preference and the
    provider's own default is the right answer. Both cases are the same on the
    wire, which is why they are the same value here.
    """
    levels = clean_levels(list(offered))
    if not levels:
        return None
    wanted = normalize(requested) or normalize(default)
    if wanted is None:
        return None
    return nearest(wanted, levels)


# -- Registry lookup ---------------------------------------------------------
#
# Mirrors `budget.context_window` exactly, and for the same reason: the value
# lives on an admin-editable row, the hot path (`_build_request`) must not grow
# an `await` for it, and `preflight` already runs once per turn with the job of
# answering everything that has to be known before the first token. Cold cache
# means no effort field, which is the behaviour every call had before this
# existed — never worse than before, better as soon as a turn has preflighted.

_TTL_SECONDS = 300
_cache: dict[str, tuple[float, tuple[str, ...], str]] = {}


def cached_support(model: str) -> tuple[tuple[str, ...], str]:
    """`(offered levels, default level)` from cache, or `((), "")` if unknown."""
    entry = _cache.get(model or "")
    return (entry[1], entry[2]) if entry else ((), "")


async def prime(model: str) -> None:
    """Fetch and cache `model`'s effort support. Never raises."""
    await support_for(model)


async def support_for(model: str) -> tuple[tuple[str, ...], str]:
    """`(offered levels, default level)` for `model`, straight from the registry."""
    if not model:
        return ((), "")

    entry = _cache.get(model)
    now = time.monotonic()
    if entry and now - entry[0] < _TTL_SECONDS:
        return (entry[1], entry[2])

    try:
        from llm.models import AIModel

        row = await AIModel.objects.filter(value=model).values(
            "effort_levels", "default_effort",
        ).afirst()
    except Exception:  # noqa: BLE001
        # Choosing an effort must not depend on the registry being reachable.
        logger.debug("[Effort] Support lookup failed for %r", model, exc_info=True)
        return ((), "")

    levels = clean_levels((row or {}).get("effort_levels"))
    default = normalize((row or {}).get("default_effort")) or ""
    # A default outside the offered set is a row that has drifted; snapping it
    # is better than sending a level the model will reject.
    if default and levels and default not in levels:
        default = nearest(default, levels) or ""
    _cache[model] = (now, levels, default)
    return (levels, default)


def clear_cache() -> None:
    """Drop every cached row. For tests and for the seed script."""
    _cache.clear()
