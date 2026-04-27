"""
Background Celery tasks for re-indexing all Knowledge Bases after an
embedder model change.

Usage (from shell or admin):
    from inference.migration_tasks import reindex_all_knowledge_bases
    reindex_all_knowledge_bases.delay()

The task is idempotent — KBs whose stored version already matches the
current EMBEDDER_VERSION are skipped automatically.
"""
import logging

from celery import shared_task
from asgiref.sync import async_to_sync

logger = logging.getLogger(__name__)


@shared_task(bind=True, time_limit=7200, soft_time_limit=7000, name='inference.reindex_all')
def reindex_all_knowledge_bases(self):
    """
    Walk every KnowledgeBase row in the DB, load its HNSW index, and
    rebuild it if the stored embedder version differs from the running one.

    Safe to run while the server is live — each KB is locked individually
    during the swap phase, and searches against the old index keep working
    until the rebuild completes.
    """
    from inference.models import KnowledgeBase
    from inference.engine import (
        EMBEDDER_VERSION,
        get_hnsw_kb,
    )

    all_kbs = list(KnowledgeBase.objects.all().values_list('id', 'name', 's3_index_key'))
    logger.info(
        f"[Reindex] Starting full re-index sweep across {len(all_kbs)} KBs "
        f"(target version: {EMBEDDER_VERSION})"
    )

    rebuilt = 0
    skipped = 0
    failed = 0

    for kb_id, kb_name, s3_key in all_kbs:
        try:
            hnsw = get_hnsw_kb(kb_id, s3_key or f'indices/kb_{kb_id}')

            async def _run():
                await hnsw.initialize()

                # Already on the right version? Skip.
                if hnsw._stored_version == EMBEDDER_VERSION:
                    return 'skip'

                await hnsw.rebuild_index()
                return 'rebuilt'

            result = async_to_sync(_run)()

            if result == 'skip':
                skipped += 1
                logger.debug(f"[Reindex] KB {kb_id} ({kb_name}) — already current, skipped.")
            else:
                rebuilt += 1
                logger.info(f"[Reindex] KB {kb_id} ({kb_name}) — rebuilt successfully.")

                # Update DB stats
                from inference.models import Document
                KnowledgeBase.objects.filter(id=kb_id).update(
                    embedding_model=EMBEDDER_VERSION.split(':')[0],
                    vector_dim=int(EMBEDDER_VERSION.split(':')[1]),
                    vector_count=hnsw.ntotal,
                    index_size_bytes=hnsw.index_size_bytes,
                )

        except Exception as e:
            failed += 1
            logger.error(f"[Reindex] KB {kb_id} ({kb_name}) — FAILED: {e}", exc_info=True)

    summary = (
        f"[Reindex] Complete — rebuilt={rebuilt}, skipped={skipped}, failed={failed} "
        f"(target={EMBEDDER_VERSION})"
    )
    logger.info(summary)
    return summary


@shared_task(bind=True, time_limit=600, soft_time_limit=540, name='inference.reindex_single_kb')
def reindex_single_knowledge_base(self, kb_id: int):
    """
    Re-index a single Knowledge Base. Useful for targeted fixes or
    admin-triggered rebuilds.
    """
    from inference.models import KnowledgeBase
    from inference.engine import EMBEDDER_VERSION, get_hnsw_kb

    try:
        kb = KnowledgeBase.objects.get(id=kb_id)
        hnsw = get_hnsw_kb(kb.id, kb.s3_index_key or f'indices/kb_{kb.id}')

        async def _run():
            await hnsw.initialize()
            await hnsw.rebuild_index()

        async_to_sync(_run)()

        KnowledgeBase.objects.filter(id=kb_id).update(
            embedding_model=EMBEDDER_VERSION.split(':')[0],
            vector_dim=int(EMBEDDER_VERSION.split(':')[1]),
            vector_count=hnsw.ntotal,
            index_size_bytes=hnsw.index_size_bytes,
        )

        msg = f"[Reindex] KB {kb_id} ({kb.name}) rebuilt → {EMBEDDER_VERSION}"
        logger.info(msg)
        return msg

    except KnowledgeBase.DoesNotExist:
        msg = f"[Reindex] KB {kb_id} not found."
        logger.warning(msg)
        return msg
    except Exception as e:
        msg = f"[Reindex] KB {kb_id} failed: {e}"
        logger.error(msg, exc_info=True)
        return msg
