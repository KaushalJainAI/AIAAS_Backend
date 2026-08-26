"""
Permanently remove everything that has sat in the recycle bin past its retention.

The other half of `inference.sweep_recycle_bin` (the Celery beat task). Both
call `inference/recycle.py::run_recycle_sweep`, the same split as
`manage.py run_due_triggers` / `manage.py send_hitl_reminders` and for the same
reason: local development runs without a broker, and a beat-only design fails by
silently never firing — which for the only thing that ever frees disk and vector
storage means the bin grows for ever with nobody noticing.

    python manage.py purge_recycle_bin
    python manage.py purge_recycle_bin --dry-run
    python manage.py purge_recycle_bin --days 0        # empty it now
"""
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = 'Purge recycle-bin rows older than the retention period.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--days', type=int, default=None,
            help='Override RECYCLE_BIN_RETENTION_DAYS. 0 purges everything trashed.')
        parser.add_argument(
            '--dry-run', action='store_true',
            help='Report what would be purged without deleting anything.')

    def handle(self, *args, **options):
        from inference.recycle import pending_purge_counts, run_recycle_sweep

        days = options['days']

        if options['dry_run']:
            due = pending_purge_counts(days)
            self.stdout.write(
                f"[DRY-RUN] Would purge {due['documents']} document(s) and "
                f"{due['folders']} folder(s) trashed before "
                f"{due['cutoff']:%Y-%m-%d %H:%M}."
            )
            return

        stats = run_recycle_sweep(days=days)
        self.stdout.write(self.style.SUCCESS(
            f"Purged {stats['purged_documents']} document(s), "
            f"{stats['purged_folders']} folder(s), "
            f"{stats['failed']} failure(s)."
        ))
