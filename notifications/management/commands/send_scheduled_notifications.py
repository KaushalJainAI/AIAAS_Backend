"""
Fire due user-asked reminders once, without Celery.

The broker-less twin of the `notifications.sweep_scheduled` beat task — local
dev runs with no Redis, and a beat-only design would silently never fire:

    python manage.py send_scheduled_notifications
    python manage.py send_scheduled_notifications --dry-run
"""

from django.core.management.base import BaseCommand
from django.utils import timezone


class Command(BaseCommand):
    help = 'Fire user-asked reminders (at-a-time and heartbeat) that are due.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Report what is due without sending anything.',
        )

    def handle(self, *args, **options):
        from notifications.models import ScheduledNotification
        from notifications.scheduled import run_scheduled_sweep

        now = timezone.now()

        if options['dry_run']:
            due = ScheduledNotification.objects.filter(
                active=True, next_run_at__isnull=False,
                next_run_at__lte=now).count()
            self.stdout.write(f'due scheduled reminders: {due}')
            return

        result = run_scheduled_sweep(now=now)
        self.stdout.write(self.style.SUCCESS(f"sent={result['sent']}"))
