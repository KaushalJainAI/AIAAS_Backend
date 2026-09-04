"""
Run the HITL reminder sweep once, without Celery.

Useful in local development (no Redis, no beat) and as the OS-cron path for
deployments that would rather not run a beat process:

    python manage.py send_hitl_reminders
    python manage.py send_hitl_reminders --dry-run
"""

from django.core.management.base import BaseCommand
from django.utils import timezone


class Command(BaseCommand):
    help = 'Send any due HITL escalations, hourly reminders and daily digests.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Report what is due without sending anything.',
        )

    def handle(self, *args, **options):
        from notifications.models import HITLReminderSchedule, NotificationPreference
        from notifications.reminders import run_reminder_sweep

        now = timezone.now()

        if options['dry_run']:
            due = HITLReminderSchedule.objects.filter(
                next_due_at__isnull=False,
                next_due_at__lte=now,
                hitl_request__status='pending',
            ).count()
            hourly = NotificationPreference.objects.filter(hourly_reminders_enabled=True).count()
            digests = NotificationPreference.objects.filter(daily_digest_enabled=True).count()
            self.stdout.write(
                f"due escalations: {due}\n"
                f"users on hourly reminders: {hourly}\n"
                f"users on daily digest: {digests}"
            )
            return

        result = run_reminder_sweep(now=now)
        self.stdout.write(self.style.SUCCESS(
            f"escalations={result['escalations']} "
            f"hourly={result['hourly']} "
            f"digests={result['digests']}"
        ))
