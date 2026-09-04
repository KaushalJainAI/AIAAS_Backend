"""
Management command: reindex_all

Rebuild Knowledge Base FAISS indices using the current embedding model. Run
after changing EMBEDDING_MODEL / EMBEDDING_DIM.

The sweep itself lives in `inference/reindex.py`, shared with the Celery task
`inference.reindex_all` — local dev has no Redis, so a Celery-only path would
silently never run.

Usage:
    python manage.py reindex_all           # Re-index all KBs
    python manage.py reindex_all --kb 42   # Re-index only KB id=42
    python manage.py reindex_all --force   # Rebuild even if already current
    python manage.py reindex_all --dry-run # Show what would be re-indexed
"""
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = (
        'Rebuild FAISS HNSW indices for all (or one) Knowledge Bases '
        'using the current embedding model.  Run after changing the model.'
    )

    def add_arguments(self, parser):
        parser.add_argument('--kb', type=int, default=None,
                            help='Re-index only this KB id (default: all)')
        parser.add_argument('--force', action='store_true',
                            help='Rebuild even KBs already on the current version.')
        parser.add_argument('--dry-run', action='store_true',
                            help='Show what would be re-indexed without doing it.')

    def handle(self, *args, **options):
        from inference.engine import EMBEDDER_VERSION
        from inference.reindex import kb_rows, reindex_many

        rows = kb_rows(options['kb'])
        if not rows:
            self.stdout.write(self.style.WARNING('No knowledge bases found.'))
            return

        self.stdout.write(
            f'Target embedder version: {EMBEDDER_VERSION}\n'
            f'Knowledge Bases to process: {len(rows)}\n'
        )

        if options['dry_run']:
            for kb_id, name, _ in rows:
                self.stdout.write(f'  [DRY-RUN] KB {kb_id}: {name}')
            return

        styles = {
            'rebuilt': self.style.SUCCESS,
            'skipped': self.style.SUCCESS,
        }

        def report(kb_id, name, outcome):
            label = {'rebuilt': 'REBUILT', 'skipped': 'UP-TO-DATE'}.get(outcome, outcome.upper())
            style = styles.get(outcome, self.style.ERROR)
            self.stdout.write(f'  KB {kb_id} ({name})... ' + style(label))

        result = reindex_many(rows, force=options['force'], on_result=report)

        self.stdout.write('')
        self.stdout.write(self.style.SUCCESS(f'Done — {result}'))
