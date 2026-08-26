"""
Keeping `KnowledgeBase.doc_count` true, wherever a document dies.

Fixing this at the call sites was the wrong altitude and it showed: patching
`document_detail`'s DELETE left `chat/sources/attachments.py` — which deletes
Documents from two more paths (`purge_rag_document`, `purge_session`) — still
drifting, and the count is read by `chat/tools/knowledge.py` and handed to the
agent as fact. Every future deleter would have had to remember too.

So the recount hangs off `post_delete`. Django sends that per instance even for
a queryset `.delete()`, so all three existing paths and any later one are
covered without knowing they exist.

Deliberately doc_count *only*. Vector columns are not touched here because at
signal time nothing has removed the document from its index yet — the caller
that also drops the vectors (`tasks.remove_document_from_kb`) refreshes those
itself afterwards, which is the only point where the number is knowable.
"""
from __future__ import annotations

import logging

from django.db.models.signals import post_delete
from django.dispatch import receiver

from .models import Document, KnowledgeBase

logger = logging.getLogger(__name__)


def recount_kb(kb_id) -> None:
    """Bring one KB's `doc_count` back in line with reality.

    A module-level function, not just the signal body, because `post_delete`
    does **not** fire when a document is *trashed* — trash is a state, not a
    delete — so the recycle-bin path in `inference/filesystem.py` has to call
    the same counter. One function, two callers, no drift.
    """
    if not kb_id:
        return

    try:
        # A cascade (user or KB deleted) takes the KB row too, and the delete
        # order between them is not guaranteed — so a missing KB is normal
        # here, not an error. `.update()` on an empty queryset is a no-op, and
        # the COUNT is skipped entirely rather than computed and thrown away.
        if not KnowledgeBase.objects.filter(id=kb_id).exists():
            return
        KnowledgeBase.objects.filter(id=kb_id).update(
            doc_count=Document.objects.filter(knowledge_base_id=kb_id).count()
        )
    except Exception:
        # A stale count must never turn a successful delete into a failure —
        # the row is already gone by the time this runs.
        logger.warning(
            'Could not recount doc_count for KB %s', kb_id, exc_info=True,
        )


@receiver(post_delete, sender=Document, dispatch_uid='inference.recount_kb_docs')
def recount_kb_documents(sender, instance: Document, **kwargs) -> None:
    """Recount the owning KB after a document row is permanently deleted."""
    recount_kb(instance.knowledge_base_id)
