"""
Management command: reindex_all

Rebuild every Knowledge Base's FAISS index using the current embedding model.
Run this after changing EMBEDDING_MODEL / EMBEDDING_DIM in engine.py.

Usage:
    python manage.py reindex_all           # Re-index all KBs
    python manage.py reindex_all --kb 42   # Re-index only KB id=42
    python manage.py reindex_all --dry-run # Show what would be re-indexed
"""
import logging

from django.core.management.base import BaseCommand
from asgiref.sync import async_to_sync

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = (
        'Rebuild FAISS HNSW indices for all (or one) Knowledge Bases '
        'using the current embedding model.  Run after changing the model.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--kb',
            type=int,
            default=None,
            help='Re-index only this KB id (default: all)',
        )
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Show what would be re-indexed without actually doing it.',
        )

    def handle(self, *args, **options):
        from inference.models import KnowledgeBase
        from inference.engine import EMBEDDER_VERSION, get_hnsw_kb

        kb_id = options['kb']
        dry_run = options['dry_run']

        if kb_id:
            qs = KnowledgeBase.objects.filter(id=kb_id)
        else:
            qs = KnowledgeBase.objects.all()

        kbs = list(qs.values_list('id', 'name', 's3_index_key'))

        if not kbs:
            self.stdout.write(self.style.WARNING('No knowledge bases found.'))
            return

        self.stdout.write(
            f'Target embedder version: {EMBEDDER_VERSION}\n'
            f'Knowledge Bases to process: {len(kbs)}\n'
        )

        if dry_run:
            for kid, kname, _ in kbs:
                self.stdout.write(f'  [DRY-RUN] KB {kid}: {kname}')
            return

        rebuilt = 0
        skipped = 0
        failed = 0

        for kid, kname, s3_key in kbs:
            self.stdout.write(f'  Processing KB {kid} ({kname})... ', ending='')
            try:
                hnsw = get_hnsw_kb(kid, s3_key or f'indices/kb_{kid}')

                async def _run():
                    await hnsw.initialize()

                    if hnsw._stored_version == EMBEDDER_VERSION:
                        return 'skip'

                    await hnsw.rebuild_index()
                    return 'rebuilt'

                result = async_to_sync(_run)()

                if result == 'skip':
                    skipped += 1
                    self.stdout.write(self.style.SUCCESS('UP-TO-DATE'))
                else:
                    rebuilt += 1
                    self.stdout.write(self.style.SUCCESS(f'REBUILT ({hnsw.ntotal} vectors)'))

                    KnowledgeBase.objects.filter(id=kid).update(
                        embedding_model=EMBEDDER_VERSION.split(':')[0],
                        vector_dim=int(EMBEDDER_VERSION.split(':')[1]),
                        vector_count=hnsw.ntotal,
                        index_size_bytes=hnsw.index_size_bytes,
                    )

            except Exception as e:
                failed += 1
                self.stdout.write(self.style.ERROR(f'FAILED: {e}'))

        self.stdout.write('')
        self.stdout.write(
            self.style.SUCCESS(
                f'Done — rebuilt={rebuilt}, skipped={skipped}, failed={failed}'
            )
        )
