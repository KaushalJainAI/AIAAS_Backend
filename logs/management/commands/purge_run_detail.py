"""
Clear the reasoning and tool payloads of finished runs past retention.

The other half of `logs.redact_old_run_detail` (the Celery beat task); both call
`logs/retention.py::run_retention_sweep`. The run rows stay — status, answer,
cost, tool names and approvals — only the detail ages out.

    python manage.py purge_run_detail
    python manage.py purge_run_detail --dry-run
    python manage.py purge_run_detail --days 30
"""
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = 'Clear reasoning and tool payloads from finished runs older than the retention period.'

    def add_arguments(self, parser):
        parser.add_argument('--days', type=int, default=None,
                            help='Override RUN_DETAIL_RETENTION_DAYS.')
        parser.add_argument('--dry-run', action='store_true',
                            help='Report what would be cleared without changing anything.')

    def handle(self, *args, **options):
        from logs.retention import pending_counts, run_retention_sweep

        if options['dry_run']:
            due = pending_counts(options['days'])
            self.stdout.write(
                f"[DRY-RUN] Would clear {due['turns']} turn(s) and {due['steps']} "
                f"step(s) from runs started before {due['cutoff']:%Y-%m-%d %H:%M}.")
            return
        done = run_retention_sweep(options['days'])
        self.stdout.write(self.style.SUCCESS(
            f"Cleared {done['turns']} turn(s) and {done['steps']} step(s) from runs "
            f"started before {done['cutoff']:%Y-%m-%d %H:%M}."))
