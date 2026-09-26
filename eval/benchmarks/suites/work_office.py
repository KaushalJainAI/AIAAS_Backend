"""
Office workday — decks, workbooks and memos, graded on the files themselves.

The other work suites grade CSV and markdown an agent wrote with `write_file`.
This one asks for what a person actually hands on: a `.xlsx` whose totals are
formulas, a `.pptx` with a real chart of the right numbers, a `.docx` memo with
a timeline table. Every grader opens the real file (`eval/office_files.py`), so
a deck that *mentions* Q3 revenue and a deck that *charts* it do not grade
alike, and a workbook with typed-in totals fails the formula checks even when
the numbers happen to be right.

As in `work_analyst.py`, expected values are computed below from the exact
fixture text by a reference implementation, and `IDEAL_OUTPUTS` renders a
correct answer with the office tools' own renderers — so the test that proves
every case passable is exercising the same code the agent calls.
"""
from __future__ import annotations

import csv
import io
import json
import random
from collections import defaultdict
from datetime import date, datetime

REGIONS = ['North', 'South', 'East', 'West']
_VARIANTS = {'North': ['North', ' north', 'NORTH'], 'South': ['South', 'south ', 'SOUTH'],
             'East': ['East', 'east', ' East '], 'West': ['West', 'WEST', 'west ']}


def _work(root: str, files: dict) -> dict:
    return {'__workspace__': {'root': root, 'files': files}}


# ─────────────────────────────────────────────── case 1: regional workbook

def _sales_rows() -> list[dict]:
    rng = random.Random(20260919)
    rows = []
    for i in range(1, 41):
        # Rows 1–3 are June and 38–40 are October: outside Q3, to be ignored.
        month = 6 if i <= 3 else 10 if i >= 38 else rng.choice([7, 8, 9])
        region = rng.choice(REGIONS)
        rows.append({
            'date': date(2026, month, rng.randint(1, 28)).isoformat(),
            'region': rng.choice(_VARIANTS[region]),
            'units': str(rng.randint(3, 60)),
            'unit_price': str(rng.choice([1200, 1450, 1800, 2250])),
        })
    return rows


SALES_ROWS = _sales_rows()


def _to_csv(rows: list[dict], fields: list[str]) -> str:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=fields, lineterminator='\n')
    writer.writeheader()
    writer.writerows(rows)
    return buf.getvalue()


SALES_CSV = _to_csv(SALES_ROWS, ['date', 'region', 'units', 'unit_price'])


def sales_reference() -> dict:
    units: dict[str, int] = defaultdict(int)
    revenue: dict[str, float] = defaultdict(float)
    for row in SALES_ROWS:
        if datetime.fromisoformat(row['date']).month not in (7, 8, 9):
            continue
        region = row['region'].strip().title()
        units[region] += int(row['units'])
        revenue[region] += int(row['units']) * float(row['unit_price'])
    return {
        'regions': {r: {'units': units[r], 'revenue': revenue[r]} for r in REGIONS},
        'total_units': sum(units[r] for r in REGIONS),
        'total_revenue': sum(revenue[r] for r in REGIONS),
    }


SALES = sales_reference()

SALES_README = """\
# Q3 regional sales workbook

Make `q3-sales.xlsx` in this folder from `sales.csv`.

Sheet **Summary**, one row per region — North, South, East, West, in that order —
with exactly these columns:

| Region | Units | Revenue |

- Units: total units sold in the region during Q3 2026 (July to September).
  Rows dated outside Q3 are ignored.
- Revenue: the sum of units × unit_price for those rows, in INR.
- Region names in the CSV are inconsistently cased and padded; they are the same
  four regions.

After the four regions add a row whose Region is `Total`, and whose Units and
Revenue cells are **formulas** that add up the rows above — not typed-in numbers.

Add a column chart of Revenue by Region to the Summary sheet.
"""


# ─────────────────────────────────────────────── case 2: board deck

METRICS = {
    'revenue_crore': {'Q1': 3.1, 'Q2': 3.6, 'Q3': 4.2},
    'churn_pct': {'Q1': 4.8, 'Q2': 4.1, 'Q3': 3.2},
    'nps': 47,
}

DECK_NOTES = """\
# Notes for the Q3 board update

Highlights (the board asked for exactly these three):
1. Opened the Pune office in August; 14 people now based there.
2. Signed our first three enterprise contracts (Tata Motors, Zomato, Nykaa).
3. Monthly churn fell for the second quarter running.

What drove Q3 revenue growth: the festive season promotion in September,
which alone brought in about 0.4 crore.

Risks: two senior engineers leaving in October; hiring plan attached separately.
"""

DECK_README = """\
# Board update deck

Make `board-update.pptx` in this folder for Thursday's board meeting.

- 5 to 8 slides, opening with a title slide.
- One slide must chart quarterly revenue in ₹ crore for Q1, Q2 and Q3 2026,
  exactly as in `metrics.json`, with the categories labelled Q1, Q2, Q3.
- Include the three highlights from `notes.md`.
- Put speaker notes on the revenue chart slide saying what drove the Q3 growth
  (it is in `notes.md`).
"""


# ─────────────────────────────────────────────── case 3: incident memo

INCIDENT_LOG = """\
2026-09-12 02:14 Alerts fire: checkout API returning 502 for all users
2026-09-12 02:19 On-call engineer acknowledges and starts investigating
2026-09-12 02:31 Load balancer health checks failing; TLS handshake errors in logs
2026-09-12 02:38 Root cause found: the API's TLS certificate expired at 02:12
2026-09-12 02:52 Renewed certificate deployed to all load balancers
2026-09-12 03:01 Checkout API healthy again; error rate back to normal
"""


def incident_reference() -> dict:
    stamps = [datetime.strptime(line[:16], '%Y-%m-%d %H:%M')
              for line in INCIDENT_LOG.strip().splitlines()]
    # From the first alert to the all-clear.
    return {'entries': len(stamps),
            'downtime': int((stamps[-1] - stamps[0]).total_seconds() // 60)}


INCIDENT = incident_reference()

MEMO_README = """\
# Incident memo

Write `incident-memo.docx` in this folder: a Word memo about the outage recorded
in `incident-log.txt`, for people who were not on call.

- Use these section headings: Summary, Timeline, Root cause, Actions.
- The Timeline section is a table with one row per log entry (Time | Event).
- The Summary states the total downtime, from the first alert to the all-clear,
  written exactly as `Downtime: N minutes`.
"""


SUITE = {
    'slug': 'work-office',
    'group': 'capability',
    'name': 'Work: Office documents',
    'agent': 'office_worker',
    'repeats': 3,
    'concurrency': 2,
    'proves': ('Given working files and a written spec, the agent produces a real workbook '
               'with live formulas, a deck with a correct native chart and a structured Word '
               'memo — the same way every time.'),
    'pass_threshold': 0.67,
    'cases': [
        {
            'name': 'Regional sales workbook',
            'goal': ('Build the Q3 regional sales workbook. Everything you need is in '
                     '{workspace}; follow README.md exactly.'),
            'input_data': _work('work/q3-workbook', {'README.md': SALES_README, 'sales.csv': SALES_CSV}),
            'graders': [
                {'type': 'file_type', 'path': 'q3-sales.xlsx', 'format': 'xlsx'},
                *[
                    {'type': 'xlsx_value', 'path': 'q3-sales.xlsx', 'sheet': 'Summary',
                     'match': {'Region': region}, 'column': 'Units',
                     'equals': values['units'], 'tolerance': 0}
                    for region, values in SALES['regions'].items()
                ],
                *[
                    {'type': 'xlsx_value', 'path': 'q3-sales.xlsx', 'sheet': 'Summary',
                     'match': {'Region': region}, 'column': 'Revenue',
                     'equals': values['revenue'], 'tolerance': 0.5}
                    for region, values in SALES['regions'].items()
                ],
                {'type': 'xlsx_value', 'path': 'q3-sales.xlsx', 'sheet': 'Summary',
                 'match': {'Region': 'Total'}, 'column': 'Units',
                 'equals': SALES['total_units'], 'tolerance': 0, 'formula': True},
                {'type': 'xlsx_value', 'path': 'q3-sales.xlsx', 'sheet': 'Summary',
                 'match': {'Region': 'Total'}, 'column': 'Revenue',
                 'equals': SALES['total_revenue'], 'tolerance': 0.5, 'formula': True},
                {'type': 'xlsx_chart', 'path': 'q3-sales.xlsx', 'sheet': 'Summary'},
                {'type': 'tool_used', 'tool': 'render_workbook'},
            ],
            'tags': ['office', 'xlsx', 'formulas'],
        },
        {
            'name': 'Board update deck',
            'goal': ('Make the Q3 board update deck. The notes, numbers and instructions are in '
                     '{workspace} (see README.md).'),
            'input_data': _work('work/board-deck', {
                'README.md': DECK_README,
                'metrics.json': json.dumps(METRICS, indent=2),
                'notes.md': DECK_NOTES,
            }),
            'graders': [
                {'type': 'file_type', 'path': 'board-update.pptx', 'format': 'pptx'},
                {'type': 'pptx_slides', 'path': 'board-update.pptx', 'min': 5, 'max': 8},
                {'type': 'pptx_chart', 'path': 'board-update.pptx',
                 'categories': ['Q1', 'Q2', 'Q3'], 'values': [3.1, 3.6, 4.2], 'tolerance': 0.001},
                {'type': 'pptx_contains', 'path': 'board-update.pptx', 'value': 'Pune'},
                {'type': 'pptx_contains', 'path': 'board-update.pptx', 'value': 'enterprise'},
                {'type': 'pptx_contains', 'path': 'board-update.pptx', 'value': 'churn'},
                {'type': 'pptx_contains', 'path': 'board-update.pptx', 'value': 'festive'},
                {'type': 'tool_used', 'tool': 'render_deck'},
            ],
            'tags': ['office', 'pptx', 'chart'],
        },
        {
            'name': 'Incident memo',
            'goal': 'Write up last night\'s outage. The log and instructions are in {workspace} (see README.md).',
            'input_data': _work('work/incident-memo', {
                'README.md': MEMO_README,
                'incident-log.txt': INCIDENT_LOG,
            }),
            'graders': [
                {'type': 'file_type', 'path': 'incident-memo.docx', 'format': 'docx'},
                {'type': 'docx_headings', 'path': 'incident-memo.docx',
                 'includes': ['Summary', 'Timeline', 'Root cause', 'Actions']},
                {'type': 'docx_table', 'path': 'incident-memo.docx', 'min_rows': INCIDENT['entries']},
                {'type': 'docx_contains', 'path': 'incident-memo.docx',
                 'value': f"Downtime: {INCIDENT['downtime']} minutes"},
                {'type': 'docx_contains', 'path': 'incident-memo.docx', 'value': 'certificate'},
                {'type': 'tool_used', 'tool': 'render_document'},
            ],
            'tags': ['office', 'docx', 'memo'],
        },
    ],
}


# ─────────────────────────────────────────────── reference outputs

def _ideal_workbook() -> bytes:
    from office import workbook

    spec = workbook.validate({'sheets': [{
        'name': 'Summary',
        'columns': [{'header': 'Region'}, {'header': 'Units', 'type': 'integer'},
                    {'header': 'Revenue', 'type': 'currency', 'currency': 'INR'}],
        'rows': [[r, SALES['regions'][r]['units'], SALES['regions'][r]['revenue']] for r in REGIONS],
        'totals': True,
        'chart': {'kind': 'column', 'title': 'Revenue by region', 'x': 'Region', 'y': ['Revenue']},
    }]})
    return workbook.render(spec)[0]


def _ideal_deck() -> bytes:
    from office import deck

    rev = METRICS['revenue_crore']
    spec = deck.validate({'title': 'Q3 board update', 'slides': [
        {'layout': 'title', 'title': 'Q3 2026 board update'},
        {'layout': 'chart', 'title': 'Revenue grew every quarter',
         'chart': {'kind': 'column', 'title': 'Revenue (₹ crore)', 'series': [
             {'name': 'Revenue', 'points': [{'x': q, 'y': v} for q, v in rev.items()]}]},
         'notes': 'Q3 growth came from the festive season promotion in September.'},
        {'layout': 'bullets', 'title': 'Highlights', 'bullets': [
            'Opened the Pune office', 'First three enterprise contracts', 'Monthly churn fell again']},
        {'layout': 'stats', 'title': 'Health', 'stats': [
            {'value': '3.2%', 'label': 'Q3 churn'}, {'value': '47', 'label': 'NPS'}]},
        {'layout': 'closing', 'title': 'Questions'},
    ]})
    return deck.render(spec, {})


def _ideal_memo() -> bytes:
    from office import document

    rows = [[line[:16], line[17:]] for line in INCIDENT_LOG.strip().splitlines()]
    spec = document.validate({'title': 'Checkout outage, 12 September', 'blocks': [
        {'type': 'heading', 'text': 'Summary'},
        {'type': 'paragraph', 'text': f"Downtime: {INCIDENT['downtime']} minutes."},
        {'type': 'heading', 'text': 'Timeline'},
        {'type': 'table', 'columns': ['Time', 'Event'], 'rows': rows},
        {'type': 'heading', 'text': 'Root cause'},
        {'type': 'paragraph', 'text': 'The TLS certificate expired.'},
        {'type': 'heading', 'text': 'Actions'},
        {'type': 'bullets', 'items': ['Alert 30 days before any certificate expires']},
    ]})
    return document.render(spec, {})


def ideal_outputs() -> dict[str, dict[str, bytes]]:
    """Rendered lazily: building them imports the office renderers."""
    return {
        'Regional sales workbook': {'q3-sales.xlsx': _ideal_workbook()},
        'Board update deck': {'board-update.pptx': _ideal_deck()},
        'Incident memo': {'incident-memo.docx': _ideal_memo()},
    }
