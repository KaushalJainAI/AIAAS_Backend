"""
Document audit — reading prose documents spread across folders, cross-checking
them, and reporting findings in a fixed structure (the GDPval-style "professional
deliverable", made gradable by a closed vocabulary).

The traps are the ones a careless reader falls into: a contract amendment that
raises the rate only from a *later* month, so the same rate is right on one
invoice and wrong on another; an arithmetic slip in a total; an invoice number
reused across months; hours over a monthly cap. Expected findings are computed
by `audit_reference()` from the same structured data the documents are rendered
from.
"""
from __future__ import annotations

import csv
import io
import json
from datetime import date, timedelta

# ───────────────────────────────────────────────────────── case 1: invoices

CONTRACTS = {
    'contracts/acme.md': """# Services agreement: Acme Analytics Pvt Ltd

Effective 2026-01-01. Acme provides data engineering support.

- Rate: ₹1,200 per hour.
- Monthly cap: 40 billable hours. Hours above the cap are not payable.
- Payment terms: net 30.
""",
    'contracts/acme-amendment-1.md': """# Amendment 1 to the Acme Analytics services agreement

The hourly rate increases from ₹1,200 to ₹1,250 per hour for work performed
**on or after 2026-09-01**. All other terms, including the monthly cap, are unchanged.
""",
    'contracts/globex.md': """# Retainer agreement: Globex Consulting

Effective 2026-04-01. Fixed monthly retainer of ₹85,000, invoiced once per month.
Each invoice must carry a unique invoice number.
""",
    'contracts/initech.md': """# Statement of work: Initech Systems

Time and materials at ₹2,000 per hour. No monthly cap.
""",
}

#: file -> (vendor, month, invoice number, hours or None, rate or None, billed total)
INVOICE_FACTS = {
    'invoices/acme-2026-07.md': ('acme', '2026-07', 'AC-0701', 42, 1200, 50400),
    'invoices/acme-2026-08.md': ('acme', '2026-08', 'AC-0801', 38, 1250, 47500),
    'invoices/acme-2026-09.md': ('acme', '2026-09', 'AC-0901', 30, 1250, 37000),
    'invoices/globex-2026-08.md': ('globex', '2026-08', 'GX-0815', None, None, 85000),
    'invoices/globex-2026-09.md': ('globex', '2026-09', 'GX-0815', None, None, 85000),
    'invoices/initech-2026-08.md': ('initech', '2026-08', 'IN-0801', 10, 2000, 20000),
}


def _rupees(n: int) -> str:
    s = str(n)
    if len(s) <= 3:
        return f'₹{s}'
    head, tail = s[:-3], s[-3:]
    groups = []
    while len(head) > 2:
        groups.insert(0, head[-2:])
        head = head[:-2]
    if head:
        groups.insert(0, head)
    return '₹' + ','.join(groups + [tail])


def _invoice_doc(vendor, month, number, hours, rate, total) -> str:
    lines = [f'# Invoice {number}', '', f'Vendor: {vendor.title()}', f'Service month: {month}', '']
    if hours is None:
        lines.append(f'Monthly retainer: {_rupees(total)}')
    else:
        lines.append(f'Hours: {hours} at {_rupees(rate)}/hour')
    lines += ['', f'**Total due: {_rupees(total)}**', '']
    return '\n'.join(lines)


INVOICES = {path: _invoice_doc(*facts) for path, facts in INVOICE_FACTS.items()}

AUDIT_README = """# Invoice audit

Check every invoice in `invoices/` against the agreements in `contracts/` (read the
amendments too).

Write `findings.json`: a JSON list with one object per invoice file:
`{"invoice": "<file name, e.g. acme-2026-07.md>", "issue": "<issue>", "overbilled": <number>}`

`issue` is exactly one of, checked in this order:
- `duplicate_number`: the invoice number was already used by an earlier invoice
- `over_cap`: billed hours exceed the monthly cap
- `wrong_rate`: the hourly rate is not the contracted rate for that service month
- `math_error`: the total does not equal hours × rate
- `ok`: none of the above

`overbilled` is the amount billed above what is actually payable under the
contract (0 for `ok`; the whole total for `duplicate_number`).
"""

_RATES = {'acme': [(date(2026, 1, 1), 1200), (date(2026, 9, 1), 1250)], 'initech': [(date(2026, 1, 1), 2000)]}
_CAPS = {'acme': 40}


def _rate_for(vendor: str, month: str) -> int:
    start = date.fromisoformat(month + '-01')
    return [rate for since, rate in _RATES[vendor] if since <= start][-1]


def audit_reference() -> dict[str, tuple[str, float]]:
    seen: set[str] = set()
    out = {}
    for path, (vendor, month, number, hours, rate, total) in sorted(INVOICE_FACTS.items(),
                                                                      key=lambda kv: kv[1][1]):
        name = path.split('/')[-1]
        if number in seen:
            out[name] = ('duplicate_number', float(total))
            continue
        seen.add(number)
        if hours is None:
            out[name] = ('ok', 0.0)
            continue
        right_rate = _rate_for(vendor, month)
        payable = min(hours, _CAPS.get(vendor, hours)) * right_rate
        if vendor in _CAPS and hours > _CAPS[vendor]:
            issue = 'over_cap'
        elif rate != right_rate:
            issue = 'wrong_rate'
        elif total != hours * rate:
            issue = 'math_error'
        else:
            issue = 'ok'
        out[name] = (issue, float(max(total - payable, 0)))
    return out


AUDIT = audit_reference()

# ──────────────────────────────────────────────────────── case 2: renewals

RENEW_TODAY = date(2026, 9, 15)
#: name -> (end date, notice days, auto_renews)
RENEWAL_FACTS = {
    'cloud-hosting': (date(2026, 11, 30), 60, True),    # notice by 10-01: inside 30 days
    'office-lease': (date(2027, 3, 31), 180, True),     # notice by 10-02: inside
    'payroll-saas': (date(2026, 12, 31), 30, True),     # notice by 12-01: outside
    'cleaning': (date(2026, 10, 10), 14, True),         # notice by 09-26: inside
    'legal-retainer': (date(2026, 10, 1), 30, False),   # no auto-renew: excluded
    'crm-licence': (date(2026, 9, 30), 30, True),       # notice by 08-31: already missed
}


def _renewal_doc(name, end, notice, auto) -> str:
    renew = ('This agreement renews automatically for a further 12 months unless '
             f'either party gives written notice at least {notice} days before the end date.'
             if auto else 'This agreement ends on the end date and does not renew.')
    return f'# {name.replace("-", " ").title()} agreement\n\nTerm ends: {end.strftime("%d %B %Y")}\n\n{renew}\n'


RENEWAL_DOCS = {f'agreements/{n}.md': _renewal_doc(n, *f) for n, f in RENEWAL_FACTS.items()}
RENEWAL_README = """# Renewal check

Today is 2026-09-15. For every agreement in `agreements/` that renews
automatically, the notice deadline is the end date minus the notice period.

Write `renewals.csv` with columns `agreement,notice_deadline,action` for every
auto-renewing agreement whose notice deadline is on or before 2026-10-15:
- `agreement` is the file name without `.md`
- `notice_deadline` is `YYYY-MM-DD`
- `action` is `missed` if the deadline is before today, otherwise `decide`

Agreements that do not auto-renew, or whose deadline is later, are left out.
"""


def renewals_reference() -> list[tuple[str, str, str]]:
    rows = []
    for name, (end, notice, auto) in sorted(RENEWAL_FACTS.items()):
        if not auto:
            continue
        deadline = end - timedelta(days=notice)
        if deadline <= date(2026, 10, 15):
            rows.append((name, deadline.isoformat(), 'missed' if deadline < RENEW_TODAY else 'decide'))
    return rows


RENEWALS = renewals_reference()


SUITE = {
    'slug': 'work-docs',
    'group': 'capability',
    'name': 'Work: Document audit',
    'agent': 'operator',
    'repeats': 3,
    'proves': 'The agent reads contracts, amendments and invoices spread across folders, applies the terms as of the right date, and reports exactly what is wrong with each document.',
    'pass_threshold': 0.67,
    'cases': [
        {
            'name': 'Invoice audit against contracts',
            'goal': 'Audit this quarter\'s vendor invoices. Everything is in {workspace}; follow README.md.',
            'input_data': {'__workspace__': {'root': 'work/invoice-audit', 'files': {
                'README.md': AUDIT_README, **CONTRACTS, **INVOICES,
            }}},
            'graders': [
                {'type': 'file_exists', 'path': 'findings.json'},
                *[
                    spec
                    for name, (issue, over) in AUDIT.items()
                    for spec in (
                        {'type': 'json_value', 'path': 'findings.json', 'select': {'invoice': name},
                         'field': 'issue', 'equals': issue},
                        {'type': 'json_value', 'path': 'findings.json', 'select': {'invoice': name},
                         'field': 'overbilled', 'equals': over, 'tolerance': 1},
                    )
                ],
            ],
            'tags': ['documents', 'dates', 'contracts'],
        },
        {
            'name': 'Contract renewal deadlines',
            'goal': 'Find which of our agreements need a renewal decision soon. Files and rules are in {workspace} (README.md).',
            'input_data': {'__workspace__': {'root': 'work/renewals', 'files': {
                'README.md': RENEWAL_README, **RENEWAL_DOCS,
            }}},
            'graders': [
                {'type': 'file_exists', 'path': 'renewals.csv'},
                {'type': 'csv_rows', 'path': 'renewals.csv', 'equals': len(RENEWALS)},
                *[
                    spec
                    for name, deadline, action in RENEWALS
                    for spec in (
                        {'type': 'csv_value', 'path': 'renewals.csv', 'match': {'agreement': name},
                         'column': 'notice_deadline', 'equals': deadline},
                        {'type': 'csv_value', 'path': 'renewals.csv', 'match': {'agreement': name},
                         'column': 'action', 'equals': action},
                    )
                ],
            ],
            'tags': ['documents', 'dates'],
        },
    ],
}


def _csv(rows, fields) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator='\n')
    writer.writerow(fields)
    writer.writerows(rows)
    return buf.getvalue()


IDEAL_OUTPUTS = {
    'Invoice audit against contracts': {'findings.json': json.dumps(
        [{'invoice': n, 'issue': i, 'overbilled': o} for n, (i, o) in AUDIT.items()], indent=2)},
    'Contract renewal deadlines': {'renewals.csv': _csv(RENEWALS, ['agreement', 'notice_deadline', 'action'])},
}
