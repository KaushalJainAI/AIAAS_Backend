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
        parser.add_argument('action', choices=['list', 'install', 'run', 'report'])
        parser.add_argument('--user', help='Email or username that owns the agents and pays for runs.')
        parser.add_argument('--suite', action='append', default=[],
                            help='Suite slug (repeatable). Default: all suites.')
        parser.add_argument('--group', choices=['capability', 'guardrail'],
                            help='Only suites in this group.')
        parser.add_argument('--provider', default='', help='Override the agents\' provider.')
        parser.add_argument('--model', default='', help='Override the agents\' model, e.g. deepseek/deepseek-v4.1-flash.')
        parser.add_argument('--notes', default='', help='Stored on each EvalRun, e.g. "after prompt change".')

    # ------------------------------------------------------------------ helpers

    def _selected(self, options) -> list[dict]:
        from eval import benchmarks

        catalogue = benchmarks.suites()
        slugs = options['suite'] or list(catalogue)
        unknown = [s for s in slugs if s not in catalogue]
        if unknown:
            raise CommandError(f'Unknown suite(s): {", ".join(unknown)}. Try `benchmark list`.')
        chosen = [catalogue[s] for s in slugs]
        if options['group']:
            chosen = [s for s in chosen if s['group'] == options['group']]
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
        runs = async_to_sync(self._sweep_all)(chosen, suites, user, options['notes']) if chosen else []

        path = write_report(scorecard.render(runs, skipped=skipped, judge=judge))
        self.stdout.write(self.style.SUCCESS(f'\nScorecard: {path}'))

    async def _sweep_all(self, chosen, suites, user, notes):
        from asgiref.sync import sync_to_async

        from eval import api as evals

        runs = []
        for definition in chosen:
            suite = suites[definition['slug']]
            self.stdout.write(self.style.MIGRATE_HEADING(
                f"\n> {definition['slug']}: {len(definition['cases'])} cases on {suite.subagent.name} ..."))
            run = await evals.run_suite_now(suite, suite.subagent, user, notes=notes)
            closed = await sync_to_async(close_paused_runs)(run)
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
        return runs

    def do_report(self, options):
        from eval.benchmarks import report as scorecard
        from eval.models import EvalRun

        user = self._user(options)
        runs = []
        for definition in self._selected(options):
            latest = (EvalRun.objects.filter(user=user, suite__name=definition['name'])
                      .select_related('suite', 'subagent').order_by('-created_at').first())
            if latest:
                runs.append(latest)
        if not runs:
            raise CommandError('No benchmark runs yet. Start with `benchmark run`.')
        path = write_report(scorecard.render(runs))
        self.stdout.write(self.style.SUCCESS(f'Scorecard: {path}'))


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
