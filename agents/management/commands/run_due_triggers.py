"""
Run the trigger sweep once, without Celery.

For local development (no Redis, no beat) and as the OS-cron path:

    python manage.py run_due_triggers
    python manage.py run_due_triggers --dry-run
"""

from django.core.management.base import BaseCommand
from django.utils import timezone


class Command(BaseCommand):
    help = 'Fire every schedule trigger that is currently due.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='List what would fire without running anything.',
        )

    def handle(self, *args, **options):
        from agents.sweep import due_triggers, run_trigger_sweep

        now = timezone.now()

        if options['dry_run']:
            # `due_triggers` rather than a second copy of the filter: a dry run
            # that asks a slightly different question than the sweep is worse
            # than no dry run at all.
            due = list(due_triggers(now))
            if not due:
                self.stdout.write('Nothing due.')
                return
            for trigger in due:
                when = trigger.queued_for or trigger.next_due_at
                owed = ' (queued)' if trigger.queued_for else ''
                self.stdout.write(
                    f'{trigger.subagent.name}: due {when:%Y-%m-%d %H:%M} UTC{owed} '
                    f'(cron "{trigger.cron}" in {trigger.tz})'
                )
            return

        counts = run_trigger_sweep(now=now)
        if not counts:
            self.stdout.write('Nothing due.')
            return
        self.stdout.write(self.style.SUCCESS(
            ' '.join(f'{k}={v}' for k, v in sorted(counts.items()))
        ))
