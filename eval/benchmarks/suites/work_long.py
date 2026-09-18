"""
Long project with delegation — the multi-stage, cross-worker workflow shape
(57% of organisations run agents on multi-stage workflows). A lead agent splits
a job across field workers with `invoke_subagent`, each worker writes its piece
into the lead's folder (the shared-workspace mechanism in `inference/vfs.py`),
and the lead assembles and checks the final deliverable.

What it stresses, all at once: delegation plumbing, the shared folder between
two agents' scopes, parallel fan-out, the lead reading files it did not write,
and arithmetic assembled from parts. Expected totals are computed from the
fixtures below.
"""
from __future__ import annotations

import csv
import io
import random

REGIONS = ('north', 'south', 'east', 'west')
PRODUCTS = ('Laptop stand', 'USB-C hub', 'Webcam', 'Desk mat', 'Monitor arm', 'Headset')


def _region_rows(region: str) -> list[dict]:
    rng = random.Random(f'long-{region}')
    rows = []
    for _ in range(18):
        product = rng.choice(PRODUCTS)
        rows.append({'product': product, 'units': rng.randint(1, 40),
                     'unit_price': f'{rng.choice([19.99, 24.5, 49.0, 12.75, 89.99, 59.0]):.2f}'})
    return rows


def _csv(rows: list[dict]) -> str:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=['product', 'units', 'unit_price'], lineterminator='\n')
    writer.writeheader()
    writer.writerows(rows)
    return buf.getvalue()


ROWS = {r: _region_rows(r) for r in REGIONS}


def region_reference(region: str) -> tuple[float, str]:
    by_product: dict[str, float] = {}
    for row in ROWS[region]:
        by_product[row['product']] = by_product.get(row['product'], 0) + row['units'] * float(row['unit_price'])
    total = round(sum(by_product.values()), 2)
    top = max(by_product, key=by_product.get)
    return total, top


REFERENCE = {r: region_reference(r) for r in REGIONS}
GRAND_TOTAL = round(sum(t for t, _ in REFERENCE.values()), 2)

BRIEF = """# Quarterly sales roll-up

There is one CSV per region in `regions/` with columns `product,units,unit_price`.

1. Delegate: one worker per region, all in a single `invoke_subagent` call.
   Each worker computes, for its region, revenue = units × unit_price summed over
   all rows, and the product with the highest revenue, and writes
   `summaries/<region>.md` in this folder containing exactly:
   `Total: <revenue to 2 decimals>` and `Top product: <product>`.
2. Then you read the four summaries and write `final.md` containing a table of
   region, total and top product, and a line `Grand total: <sum to 2 decimals>`.
"""


SUITE = {
    'slug': 'work-long',
    'group': 'capability',
    'name': 'Work: Long project with delegation',
    'agent': 'lead',
    'repeats': 3,
    'concurrency': 1,
    'proves': 'A lead agent splits a job across parallel workers that write into a shared folder, then assembles and checks a correct final deliverable from their files.',
    'pass_threshold': 0.67,
    'cases': [
        {
            'name': 'Regional roll-up via workers',
            'goal': 'Run the quarterly sales roll-up in {workspace}. Follow brief.md; the summaries must come from workers.',
            'input_data': {'__workspace__': {'root': 'work/sales-rollup', 'files': {
                'brief.md': BRIEF, **{f'regions/{r}.csv': _csv(ROWS[r]) for r in REGIONS},
            }}},
            'graders': [
                {'type': 'tool_used', 'tool': 'invoke_subagent'},
                *[
                    spec
                    for region, (total, top) in REFERENCE.items()
                    for spec in (
                        {'type': 'file_number', 'path': f'summaries/{region}.md', 'after': 'Total',
                         'equals': total, 'tolerance': 0.05},
                        {'type': 'file_contains', 'path': f'summaries/{region}.md', 'value': top},
                    )
                ],
                {'type': 'file_number', 'path': 'final.md', 'after': 'Grand total',
                 'equals': GRAND_TOTAL, 'tolerance': 0.1},
            ],
            'tags': ['delegation', 'fan-out', 'shared-folder', 'long-horizon'],
        },
    ],
}

IDEAL_OUTPUTS = {'Regional roll-up via workers': {
    **{f'summaries/{r}.md': f'Total: {t:.2f}\nTop product: {p}\n' for r, (t, p) in REFERENCE.items()},
    'final.md': f'| region | total |\n|---|---|\n\nGrand total: {GRAND_TOTAL:.2f}\n',
}}
