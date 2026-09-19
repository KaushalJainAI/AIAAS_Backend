"""
Validation shared by the three office renderers.

Every limit here **refuses rather than truncates**, and every message is
written for the model that sent the spec. A deck whose fourth bullet was
silently dropped, or whose title was cut mid-word, is a file that looks
finished and is wrong about its own content — the model can split a slide or
shorten a line if it is told to, and it cannot notice a trim it was not told
about.
"""
from __future__ import annotations

from typing import Any


class SpecError(ValueError):
    """The spec cannot be rendered. The message says what to change."""


def text(value: Any, field: str, limit: int, *, required: bool = False) -> str:
    """One string field, stripped, refused over `limit` characters."""
    if value is None:
        out = ''
    elif isinstance(value, (str, int, float)) and not isinstance(value, bool):
        out = str(value).strip()
    else:
        raise SpecError(f'{field} must be text.')
    if required and not out:
        raise SpecError(f'{field} is required.')
    if len(out) > limit:
        raise SpecError(
            f'{field} is {len(out)} characters; the limit is {limit}. '
            f'Shorten it — say less, or move the detail somewhere with room.'
        )
    return out


def items(value: Any, field: str, limit: int, *, required: bool = False) -> list:
    """One list field, refused over `limit` entries."""
    if value is None:
        value = []
    if not isinstance(value, list):
        raise SpecError(f'{field} must be a list.')
    if required and not value:
        raise SpecError(f'{field} must not be empty.')
    if len(value) > limit:
        raise SpecError(
            f'{field} has {len(value)} entries; the limit is {limit}. Split it '
            f'up rather than cramming it in.'
        )
    return value


def choice(value: Any, field: str, allowed: tuple[str, ...], default: str) -> str:
    out = str(value or default).strip().lower()
    if out not in allowed:
        raise SpecError(f'{field} must be one of: {", ".join(allowed)}. Got {out!r}.')
    return out
