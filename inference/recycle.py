"""
The recycle bin: trash, restore, and the sweep that eventually purges.

Split from `inference/filesystem.py` on purpose. This module imports
`inference.tasks` because trashing a document has to drop its vectors and
restoring has to put them back; `filesystem.py` must *not*, because that is
what makes "a move never re-indexes" a structural fact rather than a promise.
The tree lives there, the bin lives here.

**Trash is a state, not a place.** `deleted_at` plus the `LiveManager` default
means a trashed row disappears from every listing in the codebase — including
the ones written before this feature — without a single one of them being
edited. A reserved "Trash" folder would instead have made every query
responsible for remembering to exclude one magic id.

**The index is dropped at trash time, not at purge.** A trashed file that still
answers RAG queries is incomprehensible: it is gone from the UI, so the user has
no way to understand why the agent keeps citing it and no lever to stop it. The
counter-risk does not exist, because trashing touches neither `content_text` nor
`file` — restore is `process_document`, the same door every upload uses.
Mechanically it is also the only tractable option: deferring the drop would mean
a `deleted_at` post-filter in `backends/vector.py`, `backends/fulltext.py`,
`engine.py` and the platform KB, four places with no default manager to help.

The sweep is reachable both as the Celery beat task `inference.sweep_recycle_bin`
and as `manage.py purge_recycle_bin`, the same split as `agents/sweep.py` and
`notifications/reminders.py` and for the same reason: local dev has no broker,
and a beat-only design fails by silently never firing.
"""
from __future__ import annotations

import logging
from datetime import timedelta
from typing import Sequence

from asgiref.sync import async_to_sync
from django.conf import settings
from django.db import transaction
from django.db.models import F, Value
from django.db.models.functions import Replace
from django.utils import timezone

from workflow_backend.thresholds import MAX_MOVE_BATCH

from .filesystem import subtree, unique_name
from .models import Document, Folder
from .signals import recount_kb

logger = logging.getLogger(__name__)


def retention_days() -> int:
    return int(getattr(settings, 'RECYCLE_BIN_RETENTION_DAYS', 30))


# ---------------------------------------------------------------------------
# Trash
# ---------------------------------------------------------------------------

def trash(user, *, folders: Sequence[Folder] = (), documents: Sequence[Document] = ()) -> dict:
    """Move folders (with their whole subtree) and documents into the bin.

    Everything passed here has already been resolved through
    `filesystem.resolve_*`, so ownership is settled.
    """
    now = timezone.now()
    doc_ids: set = set()

    with transaction.atomic():
        for folder in folders:
            branch = subtree(folder, include_trashed=False)
            branch_ids = list(branch.values_list('pk', flat=True))
            doc_ids.update(
                Document.objects.filter(
                    user=user, folder_id__in=branch_ids,
                ).values_list('pk', flat=True)
            )
            # Descendants get deleted_at but not trashed_directly, so the trash
            # view lists one entry per delete instead of one per node.
            Folder.objects.filter(pk__in=branch_ids).update(
                deleted_at=now, trashed_directly=False, updated_at=now,
            )
            Folder.all_objects.filter(pk=folder.pk).update(trashed_directly=True)

        doc_ids.update(d.pk for d in documents)
        if doc_ids:
            Document.objects.filter(pk__in=doc_ids).update(deleted_at=now, updated_at=now)
            Document.all_objects.filter(
                pk__in=[d.pk for d in documents],
            ).update(trashed_directly=True)

    # Index removal happens after the rows are committed: a document the user
    # can still see must never be missing from search, and this ordering makes
    # the visible window empty rather than wrong.
    _deindex(doc_ids)

    return {
        'trashed_folders': len(folders),
        'trashed_documents': len(doc_ids),
        'purges_after_days': retention_days(),
    }


def _deindex(doc_ids) -> None:
    """Drop each document from whatever its KB indexed for it."""
    if not doc_ids:
        return
    from .tasks import refresh_kb_stats, remove_document_from_kb

    touched: dict = {}
    for doc in Document.all_objects.filter(pk__in=list(doc_ids)).select_related('knowledge_base'):
        kb = doc.knowledge_base
        if kb is None:
            continue
        try:
            async_to_sync(remove_document_from_kb)(kb, doc.pk)
            touched[kb.pk] = kb
        except Exception:
            # A backend that will not let go must not block the trash — the
            # sweep tries again at purge time, which is the last chance anyone
            # looks. Failing here would leave the user unable to delete.
            logger.warning('Could not de-index document %s on trash', doc.pk, exc_info=True)

    for kb in touched.values():
        # Once per KB, not once per document: this can load a FAISS index.
        refresh_kb_stats(kb)
        recount_kb(kb.pk)


# ---------------------------------------------------------------------------
# Restore
# ---------------------------------------------------------------------------

def restore(user, *, folder_ids: Sequence[int] = (), document_ids: Sequence[int] = ()) -> dict:
    """Bring rows back to where they were.

    There is deliberately **no target parameter**. A restore goes to the row's
    own recorded parent, or to root if that ancestor has since been purged —
    which means "restore into somebody else's folder" is not an attack that
    needs guarding, it is a request that cannot be expressed.
    """
    restored, refused = [], []

    folders = list(Folder.all_objects.filter(
        pk__in=list(folder_ids)[:MAX_MOVE_BATCH], user=user, deleted_at__isnull=False,
    ))
    documents = list(Document.all_objects.filter(
        pk__in=list(document_ids)[:MAX_MOVE_BATCH], user=user, deleted_at__isnull=False,
    ))

    for folder in folders:
        outcome = _restore_folder(user, folder)
        (refused if 'reason' in outcome else restored).append(outcome)

    reindex: list = []
    for doc in documents:
        outcome = _restore_document(user, doc)
        if 'reason' in outcome:
            refused.append(outcome)
        else:
            restored.append(outcome)
            reindex.append(doc.pk)

    for folder in folders:
        if any(r.get('kind') == 'folder' and r['id'] == folder.pk for r in restored):
            reindex.extend(
                Document.all_objects.filter(
                    user=user, deleted_at__isnull=True, folder_id__in=list(
                        subtree(folder).values_list('pk', flat=True)
                    ),
                ).values_list('pk', flat=True)
            )

    _reindex(reindex)
    return {'restored': restored, 'refused': refused}


def _ancestor_state(user, folder_id):
    """(exists, still_trashed) for a would-be parent."""
    if not folder_id:
        return True, False        # root always exists and is never trashed
    parent = Folder.all_objects.filter(pk=folder_id, user=user).first()
    if parent is None:
        return False, False
    return True, parent.deleted_at is not None


def _restore_folder(user, folder: Folder) -> dict:
    exists, trashed_parent = _ancestor_state(user, folder.parent_id)

    if trashed_parent:
        # Refusing here is what keeps a subtree wholly trashed or wholly live,
        # which is in turn what stops PROTECT firing halfway through a purge.
        return {'kind': 'folder', 'id': folder.pk, 'reason': 'parent_still_trashed'}

    relocated = not exists
    parent = None if relocated else folder.parent

    now = timezone.now()
    original_name, old_path = folder.name, folder.path

    with transaction.atomic():
        # Auto-suffix rather than refuse: the user did not choose this moment,
        # and a clash with something created since is not their doing. Creating
        # and renaming reject duplicates instead — there the name is deliberate.
        name = unique_name(user, parent, original_name, exclude_pk=folder.pk)
        branch_ids = list(subtree(folder).values_list('pk', flat=True))

        folder.parent = parent
        folder.name = name
        folder.deleted_at = None
        folder.trashed_directly = False
        folder.save()                       # recomputes path/depth if reparented

        if folder.path != old_path:
            # Relocated to root because an ancestor was purged — the subtree's
            # materialised paths have to follow, exactly as in filesystem.move.
            delta = folder.depth - (old_path.strip('/').count('/'))
            Folder.all_objects.filter(
                pk__in=branch_ids, path__startswith=old_path,
            ).exclude(pk=folder.pk).update(
                path=Replace('path', Value(old_path), Value(folder.path)),
                depth=F('depth') + delta,
                updated_at=now,
            )

        Folder.all_objects.filter(pk__in=branch_ids).update(
            deleted_at=None, trashed_directly=False, updated_at=now,
        )
        Document.all_objects.filter(user=user, folder_id__in=branch_ids).update(
            deleted_at=None, trashed_directly=False, updated_at=now,
        )

    return {
        'kind': 'folder', 'id': folder.pk, 'relocated': relocated,
        'folder_id': parent.pk if parent else None,
        'renamed_to': name if name != original_name else None,
    }


def _restore_document(user, doc: Document) -> dict:
    exists, trashed_parent = _ancestor_state(user, doc.folder_id)
    if trashed_parent:
        return {'kind': 'document', 'id': doc.pk, 'reason': 'parent_still_trashed'}

    relocated = not exists
    doc.folder = None if relocated else doc.folder
    doc.deleted_at = None
    doc.trashed_directly = False
    doc.save(update_fields=['folder', 'deleted_at', 'trashed_directly', 'updated_at'])

    return {
        'kind': 'document', 'id': doc.pk, 'relocated': relocated,
        'folder_id': doc.folder_id,
    }


def _reindex(doc_ids) -> None:
    """Put restored documents back in their KB, through the upload door."""
    if not doc_ids:
        return
    import threading

    from .tasks import process_document

    for doc_id in set(doc_ids):
        # Same in-process thread the upload path uses — there is no broker on
        # this deployment, so .delay() would hang and then fail.
        threading.Thread(target=process_document, args=(doc_id,), daemon=True).start()


# ---------------------------------------------------------------------------
# Purge — permanent, and the only thing that frees disk
# ---------------------------------------------------------------------------

def run_recycle_sweep(now=None, *, user=None, days=None) -> dict:
    """Permanently remove everything trashed longer ago than the retention.

    Returns counts so a caller can tell "nothing was due" from "everything
    failed". Never raises for one bad row: a document whose file has already
    vanished must not stall the whole sweep.

    Ordering is documents-first, then folders deepest-first. That is not
    tidiness — `Document.folder` is PROTECT, so the database refuses any other
    order, which is exactly why it was declared that way.
    """
    now = now or timezone.now()
    days = retention_days() if days is None else days
    cutoff = now - timedelta(days=days)

    from .tasks import refresh_kb_stats, remove_document_from_kb

    stats = {'purged_documents': 0, 'purged_folders': 0, 'failed': 0}

    docs = Document.all_objects.filter(deleted_at__isnull=False, deleted_at__lte=cutoff)
    folders = Folder.all_objects.filter(deleted_at__isnull=False, deleted_at__lte=cutoff)
    if user is not None:
        docs = docs.filter(user=user)
        folders = folders.filter(user=user)

    touched: dict = {}
    for doc in docs.select_related('knowledge_base').iterator():
        try:
            kb = doc.knowledge_base
            if kb is not None:
                # Belt and braces: the drop already happened at trash time. A
                # trash that raced a backend failure would otherwise leave
                # vectors for ever, and this is the last moment anyone looks.
                async_to_sync(remove_document_from_kb)(kb, doc.pk)
                touched[kb.pk] = kb
            if doc.file:
                try:
                    doc.file.delete(save=False)
                except (OSError, ValueError):
                    logger.warning('Could not remove file for document %s', doc.pk, exc_info=True)
            doc.delete()          # post_delete recounts; chunks + postings cascade
            stats['purged_documents'] += 1
        except Exception:
            stats['failed'] += 1
            logger.error('Failed to purge document %s', doc.pk, exc_info=True)

    for kb in touched.values():
        try:
            refresh_kb_stats(kb)
        except Exception:
            logger.warning('Could not refresh stats for KB %s', kb.pk, exc_info=True)

    # Deepest-first, so a due child is always already gone by the time its
    # parent is considered.
    for folder in folders.order_by('-depth').iterator():
        try:
            # `Folder.parent` is CASCADE and `Document.folder` is SET_NULL, so
            # deleting a folder that still holds anything would destroy rows
            # that are not due — a child trashed later than its parent — or
            # spill live documents out to the root. Skip it; the next sweep
            # takes it once its contents have aged out too. Rows are only ever
            # removed after their own retention has elapsed.
            if Folder.all_objects.filter(parent_id=folder.pk).exists():
                continue
            if Document.all_objects.filter(folder_id=folder.pk).exists():
                continue
            folder.delete()
            stats['purged_folders'] += 1
        except Exception:
            stats['failed'] += 1
            logger.error('Failed to purge folder %s', folder.pk, exc_info=True)

    if stats['purged_documents'] or stats['purged_folders'] or stats['failed']:
        logger.info(
            '[Recycle] Purged %s document(s), %s folder(s), %s failure(s) '
            '(older than %s days)',
            stats['purged_documents'], stats['purged_folders'], stats['failed'], days,
        )
    return stats


def pending_purge_counts(days=None, *, user=None) -> dict:
    """How much the next sweep would remove. Backs `--dry-run`.

    Lives here rather than in the management command so the command stays a
    dispatch wrapper and every `Folder` lookup in the app stays inside the two
    modules allowed to make one.
    """
    days = retention_days() if days is None else days
    cutoff = timezone.now() - timedelta(days=days)
    docs = Document.all_objects.filter(deleted_at__isnull=False, deleted_at__lte=cutoff)
    folders = Folder.all_objects.filter(deleted_at__isnull=False, deleted_at__lte=cutoff)
    if user is not None:
        docs, folders = docs.filter(user=user), folders.filter(user=user)
    return {'documents': docs.count(), 'folders': folders.count(),
            'cutoff': cutoff, 'days': days}


def empty_bin(user) -> dict:
    """Purge one user's bin now. The sweep with retention 0, scoped."""
    return run_recycle_sweep(user=user, days=0)


def purges_at(deleted_at):
    """When a trashed row stops being restorable."""
    return deleted_at + timedelta(days=retention_days()) if deleted_at else None
