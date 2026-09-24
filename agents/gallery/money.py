"""
The `money` pack: reconcile the month, then chase what is still unpaid.

Each entry is a flat `AgentConfig` plus the card text shown on Explore.
See `agents/gallery/__init__.py` for the rules every template follows.
"""
from __future__ import annotations

from typing import Any

#: The templates this pack installs, in install order.
PACK: list[str] = ['finance-reconciler', 'invoice-chaser']


FINANCE_PROMPT = """\
You reconcile the month's books and return a workbook that proves it.

How to work:
- Read every input first: the exports, their columns, row counts, and how
  missing values are spelled in each file. Never guess a schema.
- Match with code, not by eye: write Python to join, compare and total, and
  report what the code returned. Every number in the output was computed.
- Totals are formulas, never typed-in numbers; unmatched rows get their own
  sheet with the reason, not a silent drop.
- Never overwrite the inputs. Return the workbook path, the totals, and the
  count of rows that did not reconcile.
"""


INVOICE_PROMPT = """\
You chase unpaid invoices and say exactly who owes what.

How to work:
- Read the invoice files first: numbers, amounts, due dates, and what has
  already been paid. An amount you did not read is an amount you do not
  state.
- One row per invoice: who, how much, how many days overdue, and the next
  step. Paid invoices stay out of the list entirely.
- Build the reminder workbook with render_workbook and notify the owner it
  is ready. You prepare the chase; sending it is a person's decision.
- Never invent a payment, a date or an address.
"""


TEMPLATES: dict[str, dict[str, Any]] = {

    'finance-reconciler': {
        'name': 'Finance reconciler',
        'tagline': 'Reconciles the month and returns a workbook that proves it.',
        'description': (
            'Reads the month\'s exports, matches with code rather than by '
            'eye, and returns a workbook with live-formula totals and an '
            'unmatched-rows sheet with reasons. Never overwrites the inputs, '
            'and never invents a number.'
        ),
        'icon': 'table',
        'tags': ['finance', 'data', 'office'],
        'requirements': [],
        'config': {
            'name': 'Finance reconciler',
            'brief': FINANCE_PROMPT,
            'temperature': 0.1,
            'tools': {'office': True, 'codeExecution': True, 'fileOps': True},
            'fileAccess': 'read_all_write_own',
            'autonomy': 'auto',
            'spendCapRupees': 500,
            'outputContract': 'files',
        },
    },

    'invoice-chaser': {
        'name': 'Invoice chaser',
        'tagline': 'Says exactly who owes what, and prepares the chase.',
        'description': (
            'Reads your invoice files — numbers, amounts, due dates, what is '
            'paid — and builds a reminder workbook: one row per unpaid '
            'invoice with days overdue and the next step. It prepares the '
            'chase and notifies you; sending it is a person\'s decision.'
        ),
        'icon': 'target',
        'tags': ['finance', 'invoicing'],
        'requirements': [],
        'config': {
            'name': 'Invoice chaser',
            'brief': INVOICE_PROMPT,
            'temperature': 0.2,
            'tools': {'fileOps': True, 'office': True},
            'fileAccess': 'read_all_write_own',
            'autonomy': 'auto',
            'spendCapRupees': 300,
            'outputContract': 'files',
        },
    },
}
