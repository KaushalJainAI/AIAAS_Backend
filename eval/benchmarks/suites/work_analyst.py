"""
Analyst workday — the most common real use of agents (data analysis and report
generation, ~60% of organisations in 2026 surveys).

Each case hands the agent a folder of deliberately messy files and a written
spec, and is graded on the **files it produces**, number by number. The
expected values are not typed in: they are computed below by a reference
implementation over the exact fixture text the agent receives, so the data and
the answer key cannot drift apart. `IDEAL_OUTPUTS` is what a correct agent
would write, and `eval/tests/test_benchmarks.py` grades it to prove every case
is passable.

The mess is the point, and each kind is one a real export has:
duplicated rows, two date formats (with day-first dates that parse wrongly as
month-first), region names with stray case and whitespace, three currencies,
blank amounts, rows outside the period, and — in the reconciliation —
identical transactions that must be matched one-to-one and near-misses that
must not match at all.
"""
from __future__ import annotations

import csv
import io
import json
import random
from collections import defaultdict
from datetime import date, datetime

# ─────────────────────────────────────────────────── case 1: month-end close

FX = {'USD': 1.0, 'EUR': 1.08, 'INR': 0.012}
REGIONS = ['North', 'South', 'East', 'West']
_VARIANTS = {'North': ['North', ' north', 'NORTH'], 'South': ['South', 'south ', 'SOUTH'],
             'East': ['East', 'east', ' East '], 'West': ['West', 'WEST', 'west ']}


def _close_rows() -> list[dict]:
    rng = random.Random(20260917)
    rows = []
    for i in range(1, 61):
        month = 7 if i <= 6 else 9 if i >= 58 else 8
        day = rng.randint(1, 30 if month != 8 else 31)
        d = date(2026, month, day)
        currency = rng.choice(['USD', 'USD', 'EUR', 'INR'])
        amount = rng.randint(1500, 30000) if currency == 'INR' else round(rng.uniform(20, 400), 2)
        region = rng.choice(REGIONS)
        rows.append({
            'order_id': f'A-{1000 + i}',
            # Day-first on every third row: 03/08/2026 is 3 August, not 8 March.
            'order_date': d.strftime('%d/%m/%Y') if i % 3 == 0 else d.isoformat(),
            'region': rng.choice(_VARIANTS[region]),
            'currency': currency,
            'amount': str(amount),
            'status': rng.choices(['paid', 'refunded', 'cancelled'], [70, 15, 15])[0],
        })
    for i in (12, 33):              # blank amounts, both in August
        rows[i]['amount'] = ''
    for i in (20, 41, 44):          # exact duplicates, appended later in the file
        rows.append(dict(rows[i]))
    return rows


def _to_csv(rows: list[dict], fields: list[str]) -> str:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=fields, lineterminator='\n')
    writer.writeheader()
    writer.writerows(rows)
    return buf.getvalue()


CLOSE_ROWS = _close_rows()
CLOSE_ORDERS_CSV = _to_csv(CLOSE_ROWS, ['order_id', 'order_date', 'region', 'currency', 'amount', 'status'])
CLOSE_README = """# Month-end close: August 2026

Produce two files in this folder from `orders.csv` and `fx.json`.

## Rules (apply in this order)
1. Remove duplicate rows: if an `order_id` appears more than once, keep only its
   first occurrence. Count how many rows you removed.
2. Keep only orders whose `order_date` is in August 2026. Dates are either
   `YYYY-MM-DD` or `DD/MM/YYYY` (day first).
3. Of those, rows with a blank `amount` are skipped. Count them.
4. Normalise `region`: trim whitespace, then title case (`north` -> `North`).
5. Convert `amount` to USD by multiplying by the rate for its `currency` in `fx.json`.
6. Net revenue per region = sum of `paid` minus sum of `refunded`. `cancelled` rows are ignored.

## Outputs
- `summary.csv` with columns `region,paid_orders,net_usd`: one row per region that
  has at least one paid or refunded August order, sorted by region name.
  `paid_orders` is the number of paid orders, `net_usd` rounded to 2 decimals.
- `report.md` containing these four lines:
  - `Total net USD: <amount>`
  - `Duplicates removed: <n>`
  - `Rows skipped (blank amount): <n>`
  - `Top region: <region>`
"""


def _parse_date(text: str) -> date:
    text = text.strip()
    if '/' in text:
        return datetime.strptime(text, '%d/%m/%Y').date()
    return date.fromisoformat(text)


def close_reference() -> dict:
    seen, rows, dupes = set(), [], 0
    for row in CLOSE_ROWS:
        if row['order_id'] in seen:
            dupes += 1
            continue
        seen.add(row['order_id'])
        rows.append(row)
    august = [r for r in rows if _parse_date(r['order_date']).strftime('%Y-%m') == '2026-08']
    blank = sum(1 for r in august if not r['amount'].strip())
    net, paid = defaultdict(float), defaultdict(int)
    for r in august:
        if not r['amount'].strip() or r['status'] == 'cancelled':
            continue
        region = r['region'].strip().title()
        usd = float(r['amount']) * FX[r['currency']]
        net[region] += usd if r['status'] == 'paid' else -usd
        paid[region] += r['status'] == 'paid'
    regions = sorted(net)
    return {
        'regions': {r: {'paid_orders': paid[r], 'net_usd': round(net[r], 2)} for r in regions},
        'total': round(sum(net.values()), 2),
        'duplicates': dupes,
        'blank': blank,
        'top': max(regions, key=lambda r: net[r]),
    }


CLOSE = close_reference()

# ───────────────────────────────────────────────── case 2: bank reconciliation

LEDGER = [
    ('L-01', '2026-08-01', 'Rent August', '-1500.00'),
    ('L-02', '2026-08-02', 'Client payment Acme', '4200.00'),
    ('L-03', '2026-08-05', 'AWS invoice', '-312.45'),
    ('L-04', '2026-08-07', 'Office supplies', '-89.90'),
    ('L-05', '2026-08-10', 'Client payment Globex', '2750.00'),
    ('L-06', '2026-08-12', 'Payroll', '-6100.00'),
    ('L-07', '2026-08-15', 'Software licence', '-49.00'),
    ('L-08', '2026-08-15', 'Software licence', '-49.00'),
    ('L-09', '2026-08-18', 'Travel reimbursement', '-230.00'),
    ('L-10', '2026-08-21', 'Client payment Initech', '1800.00'),
    ('L-11', '2026-08-25', 'Insurance premium', '-410.00'),
    ('L-12', '2026-08-28', 'Consulting income', '950.00'),
]
BANK = [
    ('2026-08-01', 'RENT AUG', '-1500.00'),
    ('2026-08-04', 'ACME CORP PAYMENT', '4200.00'),
    ('2026-08-06', 'AMAZON WEB SERVICES', '-312.45'),
    ('2026-08-07', 'STAPLES 0921', '-89.90'),
    ('2026-08-11', 'GLOBEX LTD', '2750.00'),
    ('2026-08-12', 'PAYROLL RUN', '-6100.00'),
    ('2026-08-16', 'SAAS SUBSCRIPTION', '-49.00'),
    ('2026-08-17', 'SAAS SUBSCRIPTION', '-49.00'),
    ('2026-08-20', 'TRAVEL CLAIM', '-230.00'),
    ('2026-08-22', 'INITECH INC', '1800.00'),
    ('2026-08-31', 'INSURANCE CO', '-410.00'),   # 6 days after L-11: no match
    ('2026-08-29', 'CONSULTING', '950.01'),      # one cent off L-12: no match
    ('2026-08-30', 'BANK FEE', '-12.50'),        # nothing in the ledger
]
LEDGER_CSV = _to_csv([dict(zip(['id', 'date', 'memo', 'amount'], r)) for r in LEDGER],
                     ['id', 'date', 'memo', 'amount'])
BANK_CSV = _to_csv([dict(zip(['line', 'date', 'description', 'amount'], (str(i), *r)))
                    for i, r in enumerate(BANK, start=1)], ['line', 'date', 'description', 'amount'])
RECON_README = """# Bank reconciliation: August 2026

Match `bank.csv` against `ledger.csv`.

A bank line and a ledger entry match when the amounts are equal to the cent and
the dates are at most 3 days apart. Each bank line matches at most one ledger
entry and each ledger entry at most one bank line. Descriptions do not need to
match.

Write `unmatched.csv` with columns `source,ref,amount`, one row per item that has
no match: `source` is `bank` or `ledger`, `ref` is the bank `line` number or the
ledger `id`, and `amount` is its amount as given.
"""


def recon_reference() -> list[tuple[str, str, float]]:
    ledger = [(lid, date.fromisoformat(d), float(a)) for lid, d, _m, a in LEDGER]
    used: set[str] = set()
    unmatched = []
    for line, (d, _desc, a) in enumerate(BANK, start=1):
        when, amount = date.fromisoformat(d), float(a)
        candidates = sorted(
            (abs((when - ld).days), lid) for lid, ld, la in ledger
            if lid not in used and abs(la - amount) < 0.005 and abs((when - ld).days) <= 3
        )
        if candidates:
            used.add(candidates[0][1])
        else:
            unmatched.append(('bank', str(line), amount))
    unmatched += [('ledger', lid, la) for lid, _ld, la in ledger if lid not in used]
    return unmatched


RECON = recon_reference()


def _work(root: str, files: dict) -> dict:
    return {'__workspace__': {'root': root, 'files': files}}


SUITE = {
    'slug': 'work-analyst',
    'group': 'capability',
    'name': 'Work: Analyst workday',
    'agent': 'operator',
    'repeats': 3,
    'concurrency': 2,
    'proves': 'Given a folder of messy real-world exports and a written spec, the agent produces output files whose every number is right, the same way every time.',
    'pass_threshold': 0.67,
    'cases': [
        {
            'name': 'Month-end revenue close',
            'goal': (
                'The month-end close for August 2026 is due. Everything you need is in '
                '{workspace}. Follow README.md exactly and write the two output files there.'
            ),
            'input_data': _work('work/month-end-close', {
                'README.md': CLOSE_README,
                'orders.csv': CLOSE_ORDERS_CSV,
                'fx.json': json.dumps(FX, indent=2),
            }),
            'graders': [
                {'type': 'file_exists', 'path': 'summary.csv'},
                {'type': 'csv_rows', 'path': 'summary.csv', 'equals': len(CLOSE['regions'])},
                *[
                    {'type': 'csv_value', 'path': 'summary.csv', 'match': {'region': region},
                     'column': 'net_usd', 'equals': values['net_usd'], 'tolerance': 0.05}
                    for region, values in CLOSE['regions'].items()
                ],
                *[
                    {'type': 'csv_value', 'path': 'summary.csv', 'match': {'region': region},
                     'column': 'paid_orders', 'equals': values['paid_orders'], 'tolerance': 0}
                    for region, values in CLOSE['regions'].items()
                ],
                {'type': 'file_number', 'path': 'report.md', 'after': 'Total net USD',
                 'equals': CLOSE['total'], 'tolerance': 0.1},
                {'type': 'file_number', 'path': 'report.md', 'after': 'Duplicates removed',
                 'equals': CLOSE['duplicates'], 'tolerance': 0},
                {'type': 'file_number', 'path': 'report.md', 'after': 'Rows skipped (blank amount)',
                 'equals': CLOSE['blank'], 'tolerance': 0},
                {'type': 'file_regex', 'path': 'report.md', 'pattern': r'Top region:\s*\**\s*' + CLOSE['top']},
                {'type': 'tool_used', 'tool': 'execute_python'},
            ],
            'tags': ['data', 'messy', 'report'],
        },
        {
            'name': 'Bank reconciliation',
            'goal': 'Reconcile the August bank statement. The files and instructions are in {workspace} (see README.md).',
            'input_data': _work('work/bank-recon', {
                'README.md': RECON_README,
                'ledger.csv': LEDGER_CSV,
                'bank.csv': BANK_CSV,
            }),
            'graders': [
                {'type': 'file_exists', 'path': 'unmatched.csv'},
                {'type': 'csv_rows', 'path': 'unmatched.csv', 'equals': len(RECON)},
                *[
                    {'type': 'csv_value', 'path': 'unmatched.csv', 'match': {'source': source, 'ref': ref},
                     'column': 'amount', 'equals': amount, 'tolerance': 0.001}
                    for source, ref, amount in RECON
                ],
                {'type': 'tool_used', 'tool': 'execute_python'},
            ],
            'tags': ['data', 'reconciliation', 'one-to-one'],
        },
    ],
}


def _close_ideal() -> dict:
    summary = _to_csv(
        [{'region': r, 'paid_orders': v['paid_orders'], 'net_usd': f"{v['net_usd']:.2f}"}
         for r, v in CLOSE['regions'].items()],
        ['region', 'paid_orders', 'net_usd'],
    )
    report = (f"Total net USD: {CLOSE['total']:.2f}\nDuplicates removed: {CLOSE['duplicates']}\n"
              f"Rows skipped (blank amount): {CLOSE['blank']}\nTop region: {CLOSE['top']}\n")
    return {'summary.csv': summary, 'report.md': report}


#: What a correct agent writes, per case. Graded in the tests, never shown to an agent.
IDEAL_OUTPUTS = {
    'Month-end revenue close': _close_ideal(),
    'Bank reconciliation': {'unmatched.csv': _to_csv(
        [{'source': s, 'ref': r, 'amount': f'{a:.2f}'} for s, r, a in RECON], ['source', 'ref', 'amount'])},
}
