"""Adapter dataclass + CaseSpec typing. Registration is the schema."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass(frozen=True, slots=True)
class Adapter:
    slug: str
    name: str
    source_url: str
    licence: str
    hf_repo: str = ''
    revision: str = ''
    files: dict[str, str] = field(default_factory=dict)
    agent: str = 'assistant'
    group: str = 'external'
    default_sample: int = 30
    gated: bool = False
    redact_gold: bool = False
    load: Callable[..., list[dict[str, Any]]] | None = None


__all__ = ['Adapter']
