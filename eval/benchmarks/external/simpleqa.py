"""SimpleQA adapter: short factual questions; correct/incorrect/not-attempted."""
from __future__ import annotations

import json
import random
from pathlib import Path
from typing import Any


def load(cache_dir, *, sample: int = 30, seed: int = 0,
         filters: dict | None = None) -> list[dict[str, Any]]:
    src = Path(str(cache_dir)) / 'simpleqa.jsonl'
    rows = []
    if src.exists():
        for line in src.read_text(encoding='utf-8').splitlines():
            if line.strip():
                rows.append(json.loads(line))
    rng = random.Random(seed)
    rng.shuffle(rows)
    cases = []
    for i, row in enumerate(rows[:sample] if sample else rows):
        gold = str(row.get('answer', ''))
        cases.append({
            'name': f"SimpleQA {row.get('id', i)}",
            'goal': row.get('problem', ''),
            'input_data': {
                '__source__': {'dataset': 'simpleqa',
                               'revision': row.get('revision', ''),
                               'item_id': str(row.get('id', i))},
            },
            'reference': gold,
            'graders': [
                {'type': 'quasi_exact_match', 'value': gold},
                {'type': 'llm_judge',
                 'rubric': f'The correct answer is "{gold}". Does the answer state it, without contradicting it?'},
            ],
            'tags': ['external', 'simpleqa'],
        })
    return cases


__all__ = ['load']
