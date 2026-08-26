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
        from agents.models import Trigger
        from agents.sweep import run_trigger_sweep

        now = timezone.now()

        if options['dry_run']:
            due = (
                Trigger.objects
                .filter(enabled=True, mode='schedule',
                        next_due_at__isnull=False, next_due_at__lte=now)
                .select_related('subagent')
            )
            if not due:
                self.stdout.write('Nothing due.')
                return
            for trigger in due:
                self.stdout.write(
                    f'{trigger.subagent.name}: due {trigger.next_due_at:%Y-%m-%d %H:%M} '
                    f'(cron "{trigger.cron}")'
                )
            return

        counts = run_trigger_sweep(now=now)
        if not counts:
            self.stdout.write('Nothing due.')
            return
        self.stdout.write(self.style.SUCCESS(
            ' '.join(f'{k}={v}' for k, v in sorted(counts.items()))
        ))
