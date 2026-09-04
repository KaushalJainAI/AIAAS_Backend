"""
Resume or close runs left `running` by a process that went away.

Run it after a deploy or a crash, or on OS cron where there is no Celery:

    python manage.py recover_runs
    python manage.py recover_runs --dry-run

A run is judged orphaned by its own declared wall-clock limit rather than by
any process bookkeeping — see `agents/recovery.py` for why that is the test
that keeps working with more than one worker.
"""

from asgiref.sync import async_to_sync
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = 'Resume or close agent runs whose process is gone.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='List what would be recovered without touching anything.',
        )

    def handle(self, *args, **options):
        from agents.recovery import MAX_RECOVERIES_PER_SWEEP, _orphans, sweep_orphaned_runs
        from chat.turn import checkpoints

        durability = (
            'durable' if checkpoints.is_durable() else
            'NOT durable - interrupted runs can only be closed, not resumed'
        )
        self.stdout.write(f'Checkpointer: {checkpoints.active_backend} ({durability})')

        if options['dry_run']:
            # The sweep's own predicate, not a second copy of it: a dry run
            # that asks a different question than the sweep is worse than none.
            stale = async_to_sync(_orphans)(MAX_RECOVERIES_PER_SWEEP)
            if not stale:
                self.stdout.write('No orphaned runs.')
                return
            for log, allowed in stale:
                name = log.subagent.name if log.subagent else '(deleted agent)'
                self.stdout.write(
                    f'{log.execution_id} {name}: started {log.started_at:%Y-%m-%d %H:%M} '
                    f'UTC, limit {allowed}s'
                )
            return

        tally = async_to_sync(sweep_orphaned_runs)()
        if not tally['checked']:
            self.stdout.write('No orphaned runs.')
            return
        self.stdout.write(self.style.SUCCESS(
            ' '.join(f'{k}={v}' for k, v in sorted(tally.items()))
        ))
