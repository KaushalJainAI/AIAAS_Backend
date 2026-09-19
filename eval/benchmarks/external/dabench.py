"""InfiAgent-DABench adapter: data analysis over small CSVs.

Chosen over DABstep first because the sandbox cannot read files (G12): data
must pass through `read_file` (30k chars/call) into code, so items whose CSV
fits in one read are selected. DABstep's large shared files would measure G12,
not analysis.
"""
from __future__ import annotations

import json
import random
from pathlib import Path
from typing import Any


def load(cache_dir, *, sample: int = 30, seed: int = 0,
         filters: dict | None = None) -> list[dict[str, Any]]:
    src = Path(str(cache_dir)) / 'dabench.jsonl'
    rows = []
    if src.exists():
        for line in src.read_text(encoding='utf-8').splitlines():
            if line.strip():
                rows.append(json.loads(line))
    # Filter to items whose CSV fits in one read_file call.
    rows = [r for r in rows if len(str(r.get('csv', ''))) <= 30_000]
    rng = random.Random(seed)
    rng.shuffle(rows)
    cases = []
    for i, row in enumerate(rows[:sample] if sample else rows):
        cases.append({
            'name': f"DABench {row.get('id', i)}",
            'goal': row.get('question', '') + ' Read /data.csv with read_file, compute with execute_python.',
            'input_data': {
                '__workspace__': {'root': '/dab', 'files': {'/data.csv': str(row.get('csv', ''))},
                                  'watch': True},
                '__source__': {'dataset': 'dabench', 'revision': row.get('revision', ''),
                               'item_id': str(row.get('id', i))},
            },
            'reference': str(row.get('answer', '')),
            'graders': [{'type': 'numeric_match', 'value': row.get('answer', 0),
                         'tolerance': float(row.get('tolerance', 0.01))}]
            if isinstance(row.get('answer'), (int, float)) else
            [{'type': 'quasi_exact_match', 'value': str(row.get('answer', ''))}],
            'tags': ['external', 'dabench'],
        })
    return cases


__all__ = ['load']
