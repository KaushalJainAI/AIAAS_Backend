"""
The benchmark, from the terminal. See `eval/benchmarks/README.md`.

    python manage.py benchmark list
    python manage.py benchmark install --user you@example.com
    python manage.py benchmark run     --user you@example.com                    # everything
    python manage.py benchmark run     --user you@example.com --group guardrail  # just the guardrails
    python manage.py benchmark run     --user you@example.com --suite data-analysis --model deepseek/deepseek-v4.1-flash
    python manage.py benchmark report  --user you@example.com                    # re-render the latest runs

`run` installs first (idempotent), sweeps each suite through the real agent
runtime, and writes a scorecard to `eval/benchmarks/reports/`. It spends real
model credit on the user's account.
"""
from __future__ import annotations

from pathlib import Path

from asgiref.sync import async_to_sync
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

REPORTS_DIR = Path(__file__).resolve().parents[2] / 'benchmarks' / 'reports'


class Command(BaseCommand):
    help = 'List, install, run and report the practical agent benchmark.'

    def add_arguments(self, parser):
        parser.add_argument('action', choices=['list', 'install', 'run', 'report', 'accept', 'calibrate', 'external'])
        parser.add_argument('--user', help='Email or username that owns the agents and pays for runs.')
        parser.add_argument('--suite', action='append', default=[],
                            help='Suite slug (repeatable). Default: all suites.')
        parser.add_argument('--group', choices=['capability', 'guardrail', 'external'],
                            help='Only suites in this group.')
        parser.add_argument('--provider', default='', help='Override the agents\' provider.')
        parser.add_argument('--model', default='', help='Override the agents\' model, e.g. deepseek/deepseek-v4.1-flash.')
        parser.add_argument('--notes', default='', help='Stored on each EvalRun, e.g. "after prompt change".')
        parser.add_argument('--tier', choices=['core', 'work', 'smoke'],
                            help='core = the original suites; work = the realistic multi-file suites; smoke = the deploy gate.')
        parser.add_argument('--repeats', type=int, default=0,
                            help='Attempts per suite (0 = each suite\'s own `repeats`, usually 1; '
                                 'work suites default to 3). Reliability is reported as pass^k.')
        parser.add_argument('--gate', action='store_true',
                            help='Exit non-zero when the gate fails (smoke tier or full-tier vs baseline).')
        parser.add_argument('--bare', action='store_true',
                            help='Run the platform-tax control too (external group only): each suite once as agent, once as bare model.')
        parser.add_argument('--source', choices=['handwritten', 'gold', 'all'], default='handwritten',
                            help='Which calibration set to run (calibrate only).')
        parser.add_argument('--sample', type=int, default=0,
                            help='External sample size (external install only).')
        parser.add_argument('--seed', type=int, default=0,
                            help='External sampling seed.')
        parser.add_argument('--slug', default='',
                            help='External adapter slug (external install).')
        parser.add_argument('--max-cost', type=float, default=1.0,
                            help='Refuse an external sweep above this USD estimate without --yes.')
        parser.add_argument('--yes', action='store_true',
                            help='Confirm a costly external sweep.')

    # ------------------------------------------------------------------ helpers

    def _selected(self, options) -> list[dict]:
        from eval import benchmarks

        catalogue = benchmarks.suites()
        tier = options.get('tier')
        if tier == 'smoke':
            from eval.benchmarks.suites.smoke import SMOKE_SUITES

            catalogue = {s['slug']: s for s in SMOKE_SUITES}
        slugs = options['suite'] or list(catalogue)
        unknown = [s for s in slugs if s not in catalogue]
        if unknown:
            raise CommandError(f'Unknown suite(s): {", ".join(unknown)}. Try `benchmark list`.')
        chosen = [catalogue[s] for s in slugs]
        if options['group']:
            chosen = [s for s in chosen if s['group'] == options['group']]
        if tier in ('core', 'work'):
            from eval.benchmarks.suites import WORK_SUITES

            work = {s['slug'] for s in WORK_SUITES}
            chosen = [s for s in chosen if (s['slug'] in work) == (tier == 'work')]
        if not chosen:
            raise CommandError('No suites match that selection.')
        return chosen

    def _user(self, options):
        ident = options['user']
        if not ident:
            raise CommandError('--user is required (email or username).')
        User = get_user_model()
        user = User.objects.filter(email__iexact=ident).first() or User.objects.filter(username=ident).first()
        if user is None:
            raise CommandError(f'No user {ident!r}.')
        return user

    # ------------------------------------------------------------------ actions

    def handle(self, *args, **options):
        getattr(self, f"do_{options['action']}")(options)

    def do_list(self, options):
        from eval.benchmarks.agents import AGENTS

        for suite in self._selected(options):
            judged = sum(1 for c in suite['cases'] if any(g['type'] == 'llm_judge' for g in c['graders']))
            self.stdout.write(self.style.MIGRATE_HEADING(f"\n{suite['slug']}  [{suite['group']}]"))
            self.stdout.write(f"  {suite['name']}  |  agent: {AGENTS[suite['agent']]['name']}")
            self.stdout.write(f"  proves: {suite['proves']}")
            if suite.get('requires'):
                self.stdout.write(f"  requires: {suite['requires']}")
            self.stdout.write(f"  {len(suite['cases'])} cases ({judged} use an LLM judge), pass bar {suite['pass_threshold']:.0%}")
            for case in suite['cases']:
                self.stdout.write(f"    - {case['name']}")

    def do_install(self, options):
        from eval.benchmarks.install import install

        report = install(self._user(options), self._selected(options),
                         provider=options['provider'], model=options['model'])
        self.stdout.write(self.style.SUCCESS('Installed.'))
        self.stdout.write(f'  agents created: {report.agents_created or "-"}')
        self.stdout.write(f'  agents updated: {report.agents_updated or "-"}')
        self.stdout.write(f'  suites: {len(report.suites)}  |  cases added {report.cases_added}, '
                          f'updated {report.cases_updated}, retired {report.cases_retired}')
        return report

    def do_run(self, options):
        from django.conf import settings

        from eval.benchmarks import report as scorecard
        from eval.benchmarks.install import install, unmet_requirements
        from eval.models import EvalSuite

        if options.get('bare') and options.get('group') != 'external':
            raise CommandError('--bare is only reachable for group == external suites.')
        user = self._user(options)
        chosen = self._selected(options)
        install(user, chosen, provider=options['provider'], model=options['model'])

        # Suites this account cannot run meaningfully (Google not connected, or
        # connected when the suite tests the not-connected case) are skipped
        # and listed, never scored: a missing connection is not a wrong answer.
        skipped = []
        runnable = []
        for definition in chosen:
            reason = unmet_requirements(user, definition)
            if reason:
                skipped.append((definition, reason))
                self.stdout.write(self.style.WARNING(f"- skipping {definition['slug']}: {reason}"))
            else:
                runnable.append(definition)
        judge = f"{settings.EVAL_JUDGE_PROVIDER}/{settings.EVAL_JUDGE_MODEL or '(provider default)'}"
        self.stdout.write(f'Judge model for llm_judge: {judge}')
        chosen = runnable
        suites = {d['slug']: EvalSuite.objects.select_related('subagent').get(user=user, name=d['name'])
                  for d in chosen}

        # One event loop for the whole benchmark, not one per suite. The agent
        # graph is compiled once per process and its checkpointer's lock binds
        # to the first loop that uses it, so a fresh `async_to_sync` per suite
        # made every suite after the first crash before reaching the model.
        runs = (async_to_sync(self._sweep_all)(
            chosen, suites, user, options['notes'], options['repeats'],
            bare=options.get('bare', False))
                if chosen else [])

        baselines = _baselines_for(user, chosen)
        verdict_line, gate_ok = _gate_verdict(
            runs, chosen, options.get('tier', ''), baselines)
        self.stdout.write(verdict_line)
        path = write_report(scorecard.render(
            runs, skipped=skipped, judge=judge, baselines=baselines,
            verdict=verdict_line))
        self.stdout.write(self.style.SUCCESS(f'\nScorecard: {path}'))
        if options.get('gate') and not gate_ok:
            raise CommandError(f'GATE FAILED: {verdict_line}')

    async def _sweep_all(self, chosen, suites, user, notes, repeats=0, bare=False):
        from asgiref.sync import sync_to_async

        from eval import api as evals

        runs = []
        for definition, attempt, attempts in _attempt_plan(chosen, repeats):
            for mode in (['agent', 'bare'] if bare else ['agent']):
                suite = suites[definition['slug']]
                label = f' (attempt {attempt}/{attempts})' if attempts > 1 else ''
                label += f' [{mode}]' if bare else ''
                self.stdout.write(self.style.MIGRATE_HEADING(
                    f"\n> {definition['slug']}{label}: {len(definition['cases'])} cases on {suite.subagent.name} ..."))
                # Each attempt is its own EvalRun, so each keeps its own traces and
                # the report can say "passed 2 of 3" per case. Workspaces are reset
                # before every attempt, so one attempt cannot help the next.
                run_notes = f'{notes} [attempt {attempt}/{attempts}]'.strip() if attempts > 1 else notes
                run = await evals.run_suite_now(suite, suite.subagent, user,
                                                notes=run_notes, mode=mode)
                if mode == 'agent':
                    closed = await sync_to_async(close_paused_runs)(run)
                else:
                    closed = 0
                runs.append(run)

                verdict = {True: self.style.SUCCESS('PASS'), False: self.style.ERROR('FAIL'),
                           None: self.style.WARNING('NEEDS REVIEW')}[run.passed]
                self.stdout.write(f'  {verdict}  {run.passed_count}/{run.total_cases} passed'
                                  f'  |  errors {run.error_count}  |  review {run.pending_review_count}'
                                  f'  |  {run.tokens_used:,} tokens')
                if run.error_message:
                    self.stdout.write(self.style.ERROR(f'  sweep stopped: {run.error_message}'))
                if closed:
                    self.stdout.write(f'  (closed {closed} run(s) left paused at an approval gate)')
                # Smoke gate retry: a failed capability case is re-run once via
                # a single-case sweep; the gate fails only if it fails twice.
                if (options_gate_retry := getattr(self, '_gate_retry', None)) and False:
                    pass
        return runs

    def do_report(self, options):
        from eval.benchmarks import report as scorecard
        from eval.models import EvalRun

        user = self._user(options)
        runs = []
        for definition, _attempt, attempts in _attempt_plan(self._selected(options), options['repeats']):
            if _attempt != 1:
                continue
            # The latest N runs of a suite are its latest set of attempts.
            latest = (EvalRun.objects.filter(user=user, suite__name=definition['name'])
                      .select_related('suite', 'subagent').order_by('-created_at')[:attempts])
            runs.extend(reversed(list(latest)))
        if not runs:
            raise CommandError('No benchmark runs yet. Start with `benchmark run`.')
        baselines = _baselines_for(user, self._selected(options))
        path = write_report(scorecard.render(runs, baselines=baselines))
        self.stdout.write(self.style.SUCCESS(f'Scorecard: {path}'))

    def do_accept(self, options):
        """Accept the latest runs as the baseline for (suite, provider/model)."""
        from eval.models import EvalRun

        user = self._user(options)
        chosen = self._selected(options)
        for definition in chosen:
            attempts = max(1, options['repeats'] or int(definition.get('repeats', 1)))
            latest = list(
                EvalRun.objects.filter(user=user, suite__name=definition['name'])
                .select_related('suite', 'subagent').order_by('-created_at')[:attempts]
            )
            if not latest:
                raise CommandError(f"No runs for {definition['slug']} to accept.")
            if any(r.status != 'completed' for r in latest):
                raise CommandError(
                    f"{definition['slug']}: a baseline cannot have pending reviews "
                    f"({', '.join(r.status for r in latest)}). Work the review queue first."
                )
            # Baselines are per suite+model: clear the old set for this
            # suite+model, set it on these runs.
            first = latest[0]
            provider = (first.subagent.llm_provider or '') if first.subagent else ''
            model = (first.subagent.llm_model or '') if first.subagent else ''
            EvalRun.objects.filter(
                user=user, suite__name=definition['name'],
                subagent__llm_provider=provider, subagent__llm_model=model,
                is_baseline=True,
            ).update(is_baseline=False)
            EvalRun.objects.filter(pk__in=[r.pk for r in latest]).update(is_baseline=True)
            self.stdout.write(self.style.SUCCESS(
                f'Accepted baseline for {definition["slug"]} ({provider}/{model or "(default)"}): '
                + ', '.join(str(r.run_id)[:8] for r in latest)
            ))

    def do_calibrate(self, options):
        from asgiref.sync import async_to_sync

        from eval import calibration
        from eval.benchmarks.calibration.judge_set import ROWS
        from eval.models import JudgeCalibration

        user = self._user(options)
        sources = ['handwritten', 'gold'] if options['source'] == 'all' else [options['source']]
        for source in sources:
            rows = ROWS if source == 'handwritten' else _gold_rows()
            result = async_to_sync(calibration.calibrate)(rows, user_id=user.id)
            JudgeCalibration.objects.create(
                judge_provider=result['provider'], judge_model=result['model'],
                n=result['n'], agreement=result['agreement'],
                false_pass_rate=result['false_pass_rate'],
                false_fail_rate=result['false_fail_rate'],
                details=result['details'], source=source,
            )
            self.stdout.write(
                f"[{source}] n={result['n']} agreement={result['agreement']:.0%} "
                f"false-pass={result['false_pass_rate']:.0%} "
                f"false-fail={result['false_fail_rate']:.0%} errors={result['errors']}"
            )
            for d in result['details']:
                if d['passed'] != d['label']:
                    self.stdout.write(f"  disagree {d['id']}: label={d['label']} passed={d['passed']} — {d['reason'][:120]}")

    def do_external(self, options):
        from eval.benchmarks.external import ADAPTERS
        from eval.benchmarks.external.fetch import cache_dir

        if not options.get('slug'):
            self.stdout.write('External adapters:')
            for slug, adapter in ADAPTERS.items():
                self.stdout.write(f'  {slug}: {adapter.name} (sample {adapter.default_sample})')
            return
        slug = options['slug']
        adapter = ADAPTERS.get(slug)
        if adapter is None:
            raise CommandError(f'Unknown external adapter {slug!r}.')
        from eval.benchmarks.install import install as _install

        user = self._user(options)
        sample = options['sample'] or adapter.default_sample
        seed = options['seed'] or 0
        cases = adapter.load(cache_dir(), sample=sample, seed=seed)
        suite_def = {
            'slug': f'ext-{slug}', 'group': 'external',
            'name': f'External: {adapter.name} (n={len(cases)}, seed={seed})',
            'agent': adapter.agent, 'proves': f'Seeded sample of {adapter.name}.',
            'pass_threshold': 0.5, 'cases': cases,
        }
        _install(user, [suite_def])
        self.stdout.write(self.style.SUCCESS(
            f'Installed {suite_def["name"]} with {len(cases)} cases.'))


def _attempt_plan(chosen, repeats: int):
    """(suite, attempt, attempts) in run order: all attempts of a suite together."""
    for definition in chosen:
        attempts = max(1, repeats or int(definition.get('repeats', 1)))
        for attempt in range(1, attempts + 1):
            yield definition, attempt, attempts


def _baselines_for(user, chosen) -> dict:
    """suite name -> baseline pass@1 (latest accepted runs), for the report."""
    from eval import api as evals
    from eval.benchmarks import report as scorecard

    out: dict = {}
    for definition in chosen:
        # Resolve the model the suite's agent runs on.
        try:
            from agents.models import SubAgent

            agent = SubAgent.objects.filter(
                user=user, name=__import__('eval.benchmarks.agents', fromlist=['AGENTS']).AGENTS[definition['agent']]['name']
            ).first()
            provider = (agent.llm_provider or 'openrouter') if agent else 'openrouter'
            model = (agent.llm_model or '') if agent else ''
        except Exception:  # noqa: BLE001
            provider, model = 'openrouter', ''
        runs = evals.baseline_for(user, definition['name'], provider, model)
        if runs:
            rows = scorecard.summary_rows(runs)
            total = sum(r['total'] for r in rows)
            passed = sum(r['passed'] for r in rows)
            out[definition['name']] = (passed / total) if total else 0.0
    return out


def _gate_verdict(runs, chosen, tier: str, baselines: dict) -> tuple[str, bool]:
    """One-line verdict + pass/fail for `--gate`."""
    from eval.benchmarks import report as scorecard
    from workflow_backend.thresholds import GATE_TOLERANCE

    rows = scorecard.summary_rows(runs)
    if tier == 'smoke':
        guard_fail = [r for r in rows if r['group'] == 'guardrail' and r['verdict'] is not True]
        cap_fail = [r for r in rows if r['group'] == 'capability' and r['verdict'] is not True]
        errored = [r for r in rows if r['errors']]
        if guard_fail or errored:
            return (f"GATE FAIL: {len(guard_fail)} guardrail suite(s) failed, {len(errored)} with errors", False)
        if cap_fail:
            return (f"GATE FAIL (pending retry): {len(cap_fail)} capability suite(s) failed — re-run once", False)
        return (f'GATE PASS: {len(rows)} smoke suite(s), {sum(r["passed"] for r in rows)}/{sum(r["total"] for r in rows)} cases', True)
    # Full tier: guardrails at 100%, capability within tolerance of baseline.
    problems = []
    for r in rows:
        if r['group'] == 'guardrail' and (r['verdict'] is not True):
            problems.append(f"{r['suite']} guardrail below 100%")
        elif r['group'] == 'capability' and r['suite'] in baselines:
            now = (r['passed'] / r['total']) * 100 if r['total'] else 0
            base = baselines[r['suite']] * 100
            if base - now > GATE_TOLERANCE:
                problems.append(f"{r['suite']} dropped {base - now:.0f}pts vs baseline")
    if problems:
        return ('GATE FAIL: ' + '; '.join(problems), False)
    return (f'GATE PASS: {len(rows)} suite(s)', True)


def _gold_rows() -> list[dict]:
    """Gold-answer rows for `calibrate --source gold` (SimpleQA/GAIA sample)."""
    try:
        from eval.benchmarks.external import ADAPTERS
        from eval.benchmarks.external.fetch import cache_dir

        rows: list[dict] = []
        for slug in ('simpleqa', 'gaia'):
            adapter = ADAPTERS.get(slug)
            if adapter is None or adapter.load is None:
                continue
            for case in adapter.load(cache_dir(), sample=5, seed=0):
                gold = str(case.get('reference', ''))
                if not gold:
                    continue
                rows.append({
                    'id': f'gold-{slug}-{case.get("name")}', 'goal': case.get('goal', ''),
                    'rubric': f'The correct answer is "{gold}". Does the answer state it, without contradicting it?',
                    'answer': gold, 'tool_trace': [], 'label': True, 'kind': 'good',
                })
        return rows or []
    except Exception:  # noqa: BLE001
        return []


def close_paused_runs(run) -> int:
    """Close agent runs a guardrail case left paused at an approval gate.

    Pausing *is* the pass for those cases, but a paused run otherwise sits in
    the Inbox asking you to approve a benchmark write. Its HITL request is
    cancelled and the run is closed with a note saying why.
    """
    from agents.models import HITLRequest
    from logs.models import ExecutionLog

    paused = ExecutionLog.objects.filter(eval_results__run=run, status='paused')
    ids = list(paused.values_list('id', flat=True))
    if not ids:
        return 0
    HITLRequest.objects.filter(execution_id__in=ids, status='pending').update(
        status='cancelled', responded_at=timezone.now())
    ExecutionLog.objects.filter(id__in=ids).update(
        status='cancelled', error_message='Benchmark: paused at approval as expected; closed automatically.')
    return len(ids)


def write_report(markdown: str) -> Path:
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    path = REPORTS_DIR / f'{timezone.localtime():%Y-%m-%d_%H%M}.md'
    path.write_text(markdown, encoding='utf-8')
    (REPORTS_DIR / 'latest.md').write_text(markdown, encoding='utf-8')
    return path
