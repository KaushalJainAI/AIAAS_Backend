"""GAIA adapter (validation split, level 1 first). `redact_gold=True`."""
from __future__ import annotations

import json
import random
from pathlib import Path
from typing import Any


TEXT_EXTS = ('.txt', '.csv', '.json', '.py', '.md')


def load(cache_dir, *, sample: int = 30, seed: int = 0,
         filters: dict | None = None) -> list[dict[str, Any]]:
    src = Path(str(cache_dir)) / 'gaia.jsonl'
    rows = []
    if src.exists():
        for line in src.read_text(encoding='utf-8').splitlines():
            if line.strip():
                rows.append(json.loads(line))
    level = (filters or {}).get('level', 1)
    rows = [r for r in rows if int(r.get('level', 1)) == int(level)]
    rng = random.Random(seed)
    # Stratified by level is trivial here (level 1 only); shuffle for seed.
    rng.shuffle(rows)
    chosen = rows[:sample] if sample else rows
    cases, skipped = [], 0
    for i, row in enumerate(chosen):
        files: dict[str, str] = {}
        attachment = row.get('file') or {}
        name = str(attachment.get('name', ''))
        content = str(attachment.get('content', ''))
        if name and not name.lower().endswith(TEXT_EXTS):
            skipped += 1
            continue
        if name and content:
            files[f'/{name}'] = content[:200_000]
        cases.append({
            'name': f"GAIA {row.get('task_id', i)}",
            'goal': (row.get('question', '') + '\n\nEnd with a line "FINAL ANSWER: <answer>".'),
            'input_data': {
                '__workspace__': {'root': '/gaia', 'files': files, 'watch': True}
                if files else {'root': '/gaia', 'files': {}, 'watch': False},
                '__source__': {'dataset': 'gaia', 'revision': row.get('revision', ''),
                               'item_id': str(row.get('task_id', i))},
            },
            'reference': str(row.get('answer', '')),
            'graders': [{'type': 'quasi_exact_match',
                         'value': str(row.get('answer', '')), 'kind': 'string'}],
            'tags': ['external', 'gaia'],
        })
    return cases


__all__ = ['load']
