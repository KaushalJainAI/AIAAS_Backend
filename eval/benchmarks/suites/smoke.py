"""
The smoke tier: ~10 deterministic cases for the deploy gate.

Built by code, not copied, from cases tagged `'smoke'` in their home suites:
one SUITE dict per source suite (same `agent`, same `group`), named
`'Smoke: ' + source['name']`, slug `'smoke-' + source['slug']`, `repeats` 1,
`pass_threshold` copied. A case edited at home is edited in smoke too.

Not added to `ALL_SUITES`: a full run must not run the smoke cases twice.
`benchmark run --tier smoke` selects exactly these.

Rules for a smoke case (pinned by `test_benchmarks.py`): deterministic-only
(no `llm_judge`), no connector, and together touching every agent.
"""
from __future__ import annotations

from . import ALL_SUITES


def build_smoke_suites() -> list[dict]:
    out: list[dict] = []
    for source in ALL_SUITES:
        tagged = [c for c in source.get('cases', []) if 'smoke' in (c.get('tags') or [])]
        if not tagged:
            continue
        out.append({
            'slug': 'smoke-' + source['slug'],
            'group': source.get('group', 'capability'),
            'name': 'Smoke: ' + source.get('name', source['slug']),
            'agent': source['agent'],
            'proves': 'Deploy gate: ' + source.get('proves', ''),
            'pass_threshold': source.get('pass_threshold', 0.8),
            'repeats': 1,
            'cases': tagged,
        })
    return out


SMOKE_SUITES = build_smoke_suites()

__all__ = ['SMOKE_SUITES', 'build_smoke_suites']
