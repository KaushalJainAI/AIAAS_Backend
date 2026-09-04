"""
Celery entry points for re-indexing Knowledge Bases after an embedder change.

Usage (from shell or admin):
    from inference.migration_tasks import reindex_all_knowledge_bases
    reindex_all_knowledge_bases.delay()

The work itself lives in `inference/reindex.py` — these are dispatch wrappers,
the same split as `manage.py reindex_all`. Both are idempotent: a KB whose
stored version already matches `EMBEDDER_VERSION` is skipped.
"""
import logging

from celery import shared_task

logger = logging.getLogger(__name__)


@shared_task(bind=True, time_limit=7200, soft_time_limit=7000, name='inference.reindex_all')
def reindex_all_knowledge_bases(self):
    """
    Walk every KnowledgeBase row, load its HNSW index, and rebuild it if the
    stored embedder version differs from the running one.

    Safe to run while the server is live — each KB is locked individually
    during the swap phase, and searches against the old index keep working
    until the rebuild completes.
    """
    from .reindex import kb_rows, reindex_many

    rows = kb_rows()
    logger.info('[Reindex] Starting sweep across %s KBs', len(rows))
    return f'[Reindex] Complete — {reindex_many(rows)}'


@shared_task(bind=True, time_limit=600, soft_time_limit=540, name='inference.reindex_single_kb')
def reindex_single_knowledge_base(self, kb_id: int):
    """
    Re-index a single Knowledge Base. Useful for targeted fixes or
    admin-triggered rebuilds. Unlike the sweep this forces the rebuild — the
    caller named one KB, so "already current" is not a reason to do nothing.
    """
    from .reindex import kb_rows, reindex_one

    rows = kb_rows(kb_id)
    if not rows:
        msg = f'[Reindex] KB {kb_id} not found.'
        logger.warning(msg)
        return msg

    _, name, s3_key = rows[0]
    try:
        reindex_one(kb_id, s3_key, force=True)
    except Exception as exc:
        msg = f'[Reindex] KB {kb_id} failed: {exc}'
        logger.error(msg, exc_info=True)
        return msg

    from .engine import EMBEDDER_VERSION
    msg = f'[Reindex] KB {kb_id} ({name}) rebuilt → {EMBEDDER_VERSION}'
    logger.info(msg)
    return msg
