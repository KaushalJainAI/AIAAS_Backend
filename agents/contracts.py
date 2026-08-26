"""
The shape an agent's answer is required to come back in.

This is the half of the design that decides whether "configure an agent to do
it" can ever replace "write a tool that does it". `deep_research` is 55 lines
that fan out across queries, read the pages, and hand back
`{type, queries, sources, text}` — and `_on_deep_research` plus the frontend
source panels render exactly that. An agent that can only return prose cannot
stand in for it however good its prompt is, because the contract is what the UI
consumes, not the words.

A **closed registry of named contracts**, not free-form JSON Schema. The set of
shapes the UI can render is closed by construction — there is a panel per
contract or there is not — so a schema language would let an agent declare a
shape nothing can display, which is a promise the product cannot keep. Naming
them also means the agent's prompt can be told what to produce in one line.

Coercion is deliberately forgiving in one direction only: a missing optional
key is filled in, and prose that was supposed to be structured is reported as a
failure rather than silently reshaped. A contract that quietly accepts anything
is the same as having no contract.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Callable

logger = logging.getLogger(__name__)


class ContractError(ValueError):
    """The agent's answer does not satisfy the contract it was given."""


@dataclass(frozen=True, slots=True)
class Contract:
    """One named result shape."""

    name: str
    #: What the model is told to produce. Goes into the agent's system prompt.
    instruction: str
    #: Keys that must be present after coercion.
    required: tuple[str, ...]
    #: key -> default, filled in when absent.
    optional: dict[str, Any]
    #: Last-resort repair for an answer that is close but not exact.
    repair: Callable[[dict[str, Any]], dict[str, Any]] | None = None


def _repair_research(payload: dict[str, Any]) -> dict[str, Any]:
    """Accept the near-misses a model actually produces for research output."""
    # Models routinely name this `summary` or `content`.
    if 'text' not in payload:
        for alias in ('summary', 'content', 'answer'):
            if alias in payload:
                payload['text'] = payload.pop(alias)
                break
    # A bare list of URLs where objects were asked for.
    sources = payload.get('sources')
    if isinstance(sources, list):
        payload['sources'] = [
            {'url': s, 'title': s} if isinstance(s, str) else s for s in sources
        ]
    return payload


RESEARCH = Contract(
    name='research',
    instruction=(
        'Return your final answer as a single JSON object and nothing else, '
        'with these keys:\n'
        '  "text"    — your findings in full, as prose with inline reasoning\n'
        '  "queries" — the search queries you actually ran, as a list\n'
        '  "sources" — every page you used, as a list of {"url", "title"}\n'
        'Do not wrap it in a code fence. Do not add commentary around it.'
    ),
    required=('text',),
    optional={'queries': [], 'sources': [], 'type': 'deep_research'},
    repair=_repair_research,
)

EXTRACTION = Contract(
    name='extraction',
    instruction=(
        'Return your final answer as a single JSON object and nothing else, '
        'with these keys:\n'
        '  "rows"    — the extracted records, as a list of objects\n'
        '  "fields"  — the field names present on every row, as a list\n'
        '  "notes"   — anything you could not extract, as prose\n'
        'Do not wrap it in a code fence.'
    ),
    required=('rows',),
    optional={'fields': [], 'notes': '', 'type': 'extraction'},
)

#: Name -> contract. Closed on purpose; see the module docstring.
CONTRACTS: dict[str, Contract] = {c.name: c for c in (RESEARCH, EXTRACTION)}


def get(name: str) -> Contract | None:
    return CONTRACTS.get((name or '').strip().lower())


def resolve(output_schema: dict[str, Any] | None) -> Contract | None:
    """The contract a `SubAgent.output_schema` asks for, if any.

    `{}` means prose, which is the default and always valid.
    """
    if not output_schema:
        return None
    return get(str(output_schema.get('contract', '')))


def instruction_for(contract: Contract | None) -> str:
    """The block appended to the agent's system prompt."""
    if contract is None:
        return ''
    return f'\n\nOUTPUT FORMAT\n{contract.instruction}'


def _strip_fence(text: str) -> str:
    """Models fence JSON despite being told not to. Cheaper to accept than to fight."""
    stripped = text.strip()
    if not stripped.startswith('```'):
        return stripped
    body = stripped.split('\n', 1)[1] if '\n' in stripped else ''
    if body.rstrip().endswith('```'):
        body = body.rstrip()[:-3]
    return body.strip()


def coerce(answer: str, contract: Contract) -> dict[str, Any]:
    """
    Parse and complete an agent's answer against its contract.

    Raises `ContractError` when the answer is not the right shape. That is the
    point: an agent configured to produce research output and returning prose
    has failed at the thing it was configured for, and reporting that is what
    keeps "configuration replaces code" honest. Silently wrapping the prose in
    `{"text": ...}` would make every agent appear to satisfy every contract.
    """
    try:
        payload = json.loads(_strip_fence(answer or ''))
    except (json.JSONDecodeError, TypeError):
        raise ContractError(
            f'Expected a JSON object matching the "{contract.name}" contract, '
            f'got prose.'
        ) from None

    if not isinstance(payload, dict):
        raise ContractError(
            f'Expected a JSON object for the "{contract.name}" contract, '
            f'got {type(payload).__name__}.'
        )

    if contract.repair is not None:
        payload = contract.repair(dict(payload))

    missing = [key for key in contract.required if key not in payload]
    if missing:
        raise ContractError(
            f'The "{contract.name}" contract requires {", ".join(missing)}.'
        )

    for key, default in contract.optional.items():
        payload.setdefault(key, default)

    return payload
