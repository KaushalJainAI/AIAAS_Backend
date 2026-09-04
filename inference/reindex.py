"""
Re-indexing a knowledge base after an embedder change — the one implementation.

The Celery task (`migration_tasks.py`) and the management command
(`management/commands/reindex_all.py`) both used to carry their own copy of
this loop, ~90% identical and drifting; both also drove the KB with a fresh
`async_to_sync` event loop per KB.

That second point is the bug rather than the duplication. `HNSWKnowledgeBase`
guards index mutation with an `asyncio.Lock`, which binds to the first loop
that awaits it — so re-indexing a KB the ASGI loop had already touched raised
"bound to a different event loop" and was counted as a failure. `engine.py`
provides `run_kb_async` for exactly this: every KB coroutine lands on the one
`inference-kb-loop`, whoever calls it. This module is the only place that
decides how a re-index runs.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Callable, Iterable

from .engine import EMBEDDER_VERSION, get_hnsw_kb, run_kb_async

logger = logging.getLogger(__name__)


@dataclass
class ReindexReport:
    rebuilt: int = 0
    skipped: int = 0
    failed: int = 0
    failures: list = field(default_factory=list)

    def __str__(self) -> str:
        return (
            f'rebuilt={self.rebuilt}, skipped={self.skipped}, '
            f'failed={self.failed} (target={EMBEDDER_VERSION})'
        )


def kb_rows(kb_id: int | None = None) -> list[tuple[int, str, str]]:
    """(id, name, s3_index_key) for every KB, or just one."""
    from .models import KnowledgeBase

    qs = KnowledgeBase.objects.all()
    if kb_id is not None:
        qs = qs.filter(id=kb_id)
    return list(qs.values_list('id', 'name', 's3_index_key'))


def reindex_one(kb_id: int, s3_key: str = '', *, force: bool = False) -> str:
    """Rebuild one KB's vector index. Returns 'rebuilt' or 'skipped'.

    `force=False` skips KBs already on the current embedder version — which is
    what makes a sweep idempotent and safe to re-run.
    """
    hnsw = get_hnsw_kb(kb_id, s3_key or f'indices/kb_{kb_id}')

    async def _run() -> str:
        await hnsw.initialize()
        if not force and hnsw.stored_version == EMBEDDER_VERSION:
            return 'skipped'
        await hnsw.rebuild_index()
        return 'rebuilt'

    # One shared loop for every KB call in the process — see module docstring.
    outcome = run_kb_async(_run())

    if outcome == 'rebuilt':
        _write_stats(kb_id, hnsw)
    return outcome


def reindex_many(
    rows: Iterable[tuple[int, str, str]],
    *,
    force: bool = False,
    on_result: Callable[[int, str, str], None] | None = None,
) -> ReindexReport:
    """Sweep a set of KBs. Never raises — a bad KB is counted, not fatal."""
    report = ReindexReport()

    for kb_id, kb_name, s3_key in rows:
        try:
            outcome = reindex_one(kb_id, s3_key, force=force)
        except Exception as exc:
            report.failed += 1
            report.failures.append((kb_id, str(exc)))
            logger.error('[Reindex] KB %s (%s) FAILED: %s', kb_id, kb_name, exc, exc_info=True)
            if on_result:
                on_result(kb_id, kb_name, f'failed: {exc}')
            continue

        if outcome == 'skipped':
            report.skipped += 1
            logger.debug('[Reindex] KB %s (%s) already current.', kb_id, kb_name)
        else:
            report.rebuilt += 1
            logger.info('[Reindex] KB %s (%s) rebuilt.', kb_id, kb_name)
        if on_result:
            on_result(kb_id, kb_name, outcome)

    logger.info('[Reindex] Complete — %s', report)
    return report


def _write_stats(kb_id: int, hnsw) -> None:
    """Record the embedder the KB now speaks, plus its fresh vector stats."""
    from .models import KnowledgeBase

    model_name, _, dim = EMBEDDER_VERSION.partition(':')
    updates = {
        'embedding_model': model_name,
        'vector_count': hnsw.ntotal,
        'index_size_bytes': hnsw.index_size_bytes,
    }
    if dim.isdigit():
        updates['vector_dim'] = int(dim)
    KnowledgeBase.objects.filter(id=kb_id).update(**updates)
