"""
Finished sweeps -> one markdown scorecard a person can read or send.

The report is built from `EvalRun` / `EvalResult` rows, never from what the
command happened to print, so `benchmark report` can regenerate it later — and
a human review recorded afterwards (on the Evals page) changes the numbers.

It has three layers, so the same file serves a skim and an investigation:

1. **Headline** — pass rates by group, real cost, models.
2. **Suites** — one row per suite, then every case with why it failed.
3. **Case details** — for every case: the goal, each grader's verdict and
   detail, the tools the agent really called, and an excerpt of the answer.

Cost is the provider's reported `ExecutionLog.cost_usd`, not the spend cap's
flat token rate: that rate is a blast-radius control and overstates cheap models
and understates dear ones. Judge calls are **not** in it — they are billed to the
same key but no run records them — and the report says so.
"""
from __future__ import annotations

from decimal import Decimal

from django.utils import timezone

from .suites import ALL_SUITES

_BY_NAME = {s['name']: s for s in ALL_SUITES}

#: For the rupee line only. Approximate on purpose, and labelled as such in the
#: report: a benchmark is not the place to track an exchange rate.
USD_TO_INR = Decimal('88')
ANSWER_EXCERPT_CHARS = 700


def _mark(passed) -> str:
    return {True: 'PASS', False: 'FAIL', None: 'REVIEW'}[passed]


def _secs(ms) -> str:
    return '-' if ms is None else f'{ms / 1000:.1f}s'


def _cell(text: str, limit: int = 90) -> str:
    text = ' '.join((text or '').split()).replace('|', '\\|')
    return text if len(text) <= limit else text[: limit - 1] + '…'


def _pct(part: int, whole: int) -> str:
    return f'{100 * part / whole:.0f}%' if whole else '-'


def _usd(value) -> str:
    value = Decimal(value or 0)
    return f'${value:.4f} (≈ ₹{value * USD_TO_INR:.2f})'


def _verdict(result) -> str:
    if result.status == 'error':
        return 'ERROR'
    if result.status == 'skipped':
        return 'SKIPPED'
    return _mark(result.final_passed)


def _failed_checks(result) -> str:
    if result.status == 'error':
        return 'run errored: ' + (result.error_message or '')
    if result.status == 'skipped':
        return 'skipped: ' + (result.error_message or '')
    misses = [f"{g['type']}: {g.get('detail') or 'failed'}" for g in (result.grades or []) if not g.get('passed')]
    return '; '.join(misses)


def _tools_called(result) -> list[str]:
    execution = result.execution
    trace = ((execution.output_data or {}).get('tool_trace') or []) if execution else []
    return [str(call.get('tool') or call.get('name') or '?') for call in trace]


def _cost(result) -> Decimal:
    return Decimal(result.execution.cost_usd or 0) if result.execution else Decimal(0)


def summary_rows(runs) -> list[dict]:
    rows = []
    for run in runs:
        definition = _BY_NAME.get(run.suite.name, {})
        results = list(run.results.select_related('execution', 'review').order_by('id'))
        rows.append({
            'suite': run.suite.name,
            'slug': definition.get('slug', ''),
            'group': definition.get('group', '?'),
            'proves': definition.get('proves', run.suite.description),
            'agent': run.subagent.name if run.subagent else '-',
            'model': (f"{run.subagent.llm_provider}/{run.subagent.llm_model or '(provider default)'}"
                      if run.subagent else '-'),
            'passed': run.passed_count,
            'total': run.total_cases,
            'errors': run.error_count,
            'review': run.pending_review_count,
            'score': run.score,
            'threshold': run.suite.pass_threshold,
            'verdict': run.passed,
            'status': run.status,
            'tokens': run.tokens_used,
            'duration_ms': run.duration_ms,
            'cost': sum((_cost(r) for r in results), Decimal(0)),
            'results': results,
            'run': run,
        })
    return rows


def _group_line(rows, group: str, label: str) -> str:
    chosen = [r for r in rows if r['group'] == group]
    if not chosen:
        return f'- **{label}:** not run'
    cases = sum(r['total'] for r in chosen)
    passed = sum(r['passed'] for r in chosen)
    held = sum(1 for r in chosen if r['verdict'] is True)
    return (f'- **{label}:** {passed}/{cases} cases ({_pct(passed, cases)}), '
            f'{held}/{len(chosen)} suites at their pass bar')


def _attention(rows) -> list[str]:
    """Every case that did not pass, grouped by what to do about it."""
    fails, reviews, errors = [], [], []
    for r in rows:
        for result in r['results']:
            verdict = _verdict(result)
            item = f"- **{r['suite']} › {result.case_name}**: {_cell(_failed_checks(result), 220) or 'graders could not decide'}"
            if verdict == 'FAIL':
                fails.append(item)
            elif verdict == 'REVIEW':
                reviews.append(item)
            elif verdict == 'ERROR':
                errors.append(item)
    out = ['## Needs attention', '']
    if not (fails or reviews or errors):
        return out + ['Nothing: every case passed.', '']
    if fails:
        out += [f'### Failed ({len(fails)})', '', *fails, '']
    if reviews:
        out += [f'### Waiting for a human verdict ({len(reviews)})', '',
                'Answer these on the Evals page, then run `manage.py benchmark report`.', '',
                *reviews, '']
    if errors:
        out += [f'### Errored: the agent never answered ({len(errors)})', '', *errors, '']
    return out


def _case_detail(r, result) -> list[str]:
    tools = _tools_called(result)
    lines = [
        f"#### {result.case_name} — {_verdict(result)}",
        '',
        f"- **Goal:** {_cell(result.goal, 400)}",
        f"- **Tools really called:** {', '.join(f'`{t}`' for t in tools) if tools else 'none'}",
        f"- **Time / tokens / cost:** {_secs(result.duration_ms)} · {result.tokens:,} · {_usd(_cost(result))}",
    ]
    if result.execution:
        lines.append(f"- **Execution:** `{result.execution.execution_id}` (open on /runs)")
    review = getattr(result, 'review', None)
    if review is not None:
        lines.append(f"- **Human verdict:** {review.verdict}"
                     + (f" — {_cell(review.comment, 200)}" if review.comment else ''))
    lines += ['', '| Grader | Passed | Detail |', '|---|---|---|']
    for grade in result.grades or []:
        lines.append(f"| `{grade['type']}` | {'✅' if grade.get('passed') else '❌'} | "
                     f"{_cell(grade.get('detail') or '', 160)} |")
    if result.status == 'error':
        lines.append(f"| (run) | ❌ | {_cell(result.error_message, 160)} |")
    answer = (result.answer or '').strip()
    if answer:
        excerpt = answer[:ANSWER_EXCERPT_CHARS]
        more = '…' if len(answer) > ANSWER_EXCERPT_CHARS else ''
        lines += ['', '<details><summary>Answer excerpt</summary>', '', '```text',
                  excerpt.replace('```', "'''") + more, '```', '', '</details>']
    return lines + ['']


def render(runs, *, skipped=(), judge: str = '') -> str:
    runs = list(runs)
    skipped = list(skipped)
    rows = summary_rows(runs)
    total_cases = sum(r['total'] for r in rows)
    total_passed = sum(r['passed'] for r in rows)
    total_tokens = sum(r['tokens'] for r in rows)
    total_cost = sum((r['cost'] for r in rows), Decimal(0))
    total_ms = sum(r['duration_ms'] or 0 for r in rows)
    agent_models = sorted({r['model'] for r in rows})

    out = [
        '# AIAAS benchmark scorecard',
        '',
        f"Generated {timezone.localtime():%Y-%m-%d %H:%M %Z}.",
        '',
        '## Headline',
        '',
        f'- **Cases passed:** {total_passed} / {total_cases} ({_pct(total_passed, total_cases)})',
        _group_line(rows, 'capability', 'Capability'),
        _group_line(rows, 'guardrail', 'Guardrails'),
        f"- **Agent model:** {', '.join(f'`{m}`' for m in agent_models) or '-'}",
        *([f'- **Judge model:** `{judge}`'] if judge else []),
        f'- **Real cost (agent runs):** {_usd(total_cost)} for {total_tokens:,} tokens. '
        'Judge calls are billed to the same key but not recorded, so they are not included.',
        f'- **Wall time:** {_secs(total_ms)}',
        *([f'- **Skipped:** {len(skipped)} suite(s) this account cannot run (listed below)'] if skipped else []),
        '',
        '## Suites',
        '',
        '| Suite | Group | Verdict | Passed | Score / bar | Errors | Review | Time | Tokens | Cost |',
        '|---|---|---|---|---|---|---|---|---|---|',
    ]
    for r in rows:
        score = '-' if r['score'] is None else f"{r['score']:.2f}"
        out.append(
            f"| {r['suite']} | {r['group']} | {_mark(r['verdict'])} | {r['passed']}/{r['total']} | "
            f"{score} / {r['threshold']:.2f} | {r['errors']} | {r['review']} | "
            f"{_secs(r['duration_ms'])} | {r['tokens']:,} | ${r['cost']:.4f} |"
        )
    out.append('')
    out += _attention(rows)

    out += ['## Results by suite', '']
    for r in rows:
        run = r['run']
        out += [
            f"### {r['suite']} — {_mark(r['verdict'])}",
            '',
            f"*What a pass proves:* {r['proves']}",
            '',
            f"Agent `{r['agent']}` · model `{r['model']}` · run `{run.run_id}` · status `{r['status']}`",
            '',
            '| Case | Result | Why it failed | Time | Tokens |',
            '|---|---|---|---|---|',
        ]
        for result in r['results']:
            out.append(
                f'| {_cell(result.case_name, 50)} | {_verdict(result)} | {_cell(_failed_checks(result))} | '
                f'{_secs(result.duration_ms)} | {result.tokens:,} |'
            )
        out.append('')

    if skipped:
        out += ['## Skipped', '', '| Suite | Why |', '|---|---|']
        out += [f"| {d['name']} | {_cell(reason, 140)} |" for d, reason in skipped]
        out.append('')

    out += ['## Case details', '']
    for r in rows:
        out += [f"### {r['suite']}", '']
        for result in r['results']:
            out += _case_detail(r, result)

    out += [
        '## How to read this',
        '',
        '- **PASS / FAIL** on a case is every grader agreeing / at least one disagreeing.',
        '- **REVIEW** means the automatic graders could not decide; answer it on the Evals page and regenerate with `manage.py benchmark report`.',
        '- **Skipped** suites need something this account does not have (usually Google connected). They are not failures.',
        '- **ERROR** means the agent never answered (provider down, missing key, spend cap). It is an outage, not a wrong answer.',
        '- **Tools really called** is what dispatched, from the run trace. A `MOCK` line in an answer is a proposal and never appears there.',
        f'- Rupee figures use an approximate rate of ₹{USD_TO_INR} per US dollar.',
        '',
    ]
    return '\n'.join(out)
