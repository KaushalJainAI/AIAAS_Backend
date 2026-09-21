"""
Purge inbound messages past retention, without Celery.
"""
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = 'Delete inbound messaging rows older than MESSAGING_RETENTION_DAYS.'

    def handle(self, *args, **options):
        from messaging.retention import purge_expired

        self.stdout.write(self.style.SUCCESS(f'purged={purge_expired()}'))
