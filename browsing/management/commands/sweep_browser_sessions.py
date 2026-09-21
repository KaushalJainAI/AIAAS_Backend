"""
Close idle and over-age browser sessions, without Celery.

The same entry point the beat task wraps — local dev has no Redis and a
beat-only design would silently never fire:

    python manage.py sweep_browser_sessions
"""

from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = 'Expire idle and over-age browser sessions.'

    def handle(self, *args, **options):
        from browsing.sessions import sweep

        result = sweep()
        self.stdout.write(self.style.SUCCESS(
            f"idle_expired={result['idle_expired']} "
            f"aged_expired={result['aged_expired']}"
        ))
