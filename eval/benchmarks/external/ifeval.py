"""IFEval adapter: verifiable formatting instructions, deterministic."""
from __future__ import annotations

import json
import random
from pathlib import Path
from typing import Any


def load(cache_dir, *, sample: int = 30, seed: int = 0,
         filters: dict | None = None) -> list[dict[str, Any]]:
    """Load IFEval rows as case dicts. Reads a JSONL file in `cache_dir`.

    In production `fetch.py` downloads `google/IFEval` from HF first; in unit
    tests the caller passes a dir holding a tiny synthetic `ifeval.jsonl`.
    """
    src = Path(str(cache_dir)) / 'ifeval.jsonl'
    rows = []
    if src.exists():
        for line in src.read_text(encoding='utf-8').splitlines():
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    rows = [r for r in rows if r.get('prompt')]
    rng = random.Random(seed)
    rng.shuffle(rows)
    chosen = rows[:sample] if sample else rows
    cases = []
    for i, row in enumerate(chosen):
        instructions = row.get('instruction_ids') or ['keyword_presence']
        cases.append({
            'name': f"IFEval {row.get('key', i)}",
            'goal': row.get('prompt', ''),
            'input_data': {
                '__source__': {'dataset': 'ifeval', 'revision': row.get('revision', ''),
                               'item_id': str(row.get('key', i))},
            },
            'reference': '',
            'graders': [{'type': 'ifeval_check',
                         'instruction_ids': instructions,
                         'kwargs': row.get('kwargs', {})}],
            'tags': ['external', 'ifeval'],
        })
    return cases


__all__ = ['load']
