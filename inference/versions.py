"""
Version history: what a file held before each overwrite.

Every path that overwrites a file calls `snapshot` first — the apps' saves
(`office_edit.replace_bytes` / `save_text`), the agent tools (`vfs.write_file`,
`vfs.edit_file`, `vfs.write_binary(overwrite=True)`) and `restore` itself — so
one table answers "what did this say before", whoever changed it.

Three rules carry it.

* **Coalesced per source.** Saves from the same source inside
  `FILE_VERSION_COALESCE_SECONDS` share one version: the one taken *before*
  the burst began, which is the state a person means by "before I started".
  An autosaving editor would otherwise fill the cap with near-identical rows
  in a minute. A burst is the *latest* version's source: any version from
  another source in between (an agent write, a restore) ends it, so the next
  app save keeps what the agent wrote — the write people most want to undo.
* **Bounded.** `FILE_VERSIONS_KEPT` per file, oldest deleted with its blob;
  files over `FILE_VERSION_MAX_BYTES` are not versioned at all.
* **Best-effort.** Failing to keep a version never fails the save it
  precedes: a save that refuses because history is full is worse than a save
  without history. Failures are logged.
"""
from __future__ import annotations

import logging
from datetime import timedelta

from django.core.files.base import ContentFile
from django.utils import timezone

from .models import Document, DocumentVersion

logger = logging.getLogger(__name__)


def _limits():
    from workflow_backend import thresholds as t

    return (t.FILE_VERSIONS_KEPT, t.FILE_VERSION_COALESCE_SECONDS,
            t.FILE_VERSION_MAX_BYTES)


def _current_bytes(doc: Document) -> bytes | None:
    if not doc.file:
        return None
    try:
        with doc.file.open('rb') as fh:
            return fh.read()
    except Exception:  # noqa: BLE001 — a missing blob is recorded as text only
        return None


def snapshot(doc: Document, source: str = 'app', *, force: bool = False) -> DocumentVersion | None:
    """Keep `doc` as it is *now* (before the caller overwrites it)."""
    if doc.pk is None:
        return None
    kept, window, max_bytes = _limits()
    try:
        if not force:
            # Coalesce only into a burst that is still running: the file's
            # newest version must be this source's and recent. Filtering by
            # source alone reached past an agent write in between, and the
            # next app save then kept no copy of what the agent wrote.
            latest = (DocumentVersion.objects.filter(document=doc)
                      .order_by('-created_at', '-id')
                      .values('source', 'created_at').first())
            if (latest is not None and latest['source'] == source
                    and latest['created_at'] >= timezone.now() - timedelta(seconds=window)):
                return None
        if (doc.file_size or 0) > max_bytes:
            return None

        data = _current_bytes(doc)
        if data is None and not (doc.content_text or '').strip():
            return None  # nothing worth restoring
        spec = (doc.metadata or {}).get('spec')
        version = DocumentVersion(
            document=doc, source=source, name=doc.name,
            content_text=doc.content_text or '',
            file_size=len(data) if data is not None else len((doc.content_text or '').encode('utf-8')),
            spec=spec if isinstance(spec, dict) else None,
        )
        if data is not None:
            version.file.save(doc.name, ContentFile(data), save=False)
        try:
            version.save()
        except Exception:
            if version.file:
                version.file.delete(save=False)
            raise
        _prune(doc, kept)
        return version
    except Exception:  # noqa: BLE001 — see module note: never fail the save
        logger.exception('Could not keep a version of document %s', doc.pk)
        return None


def _prune(doc: Document, kept: int) -> None:
    stale = list(DocumentVersion.objects.filter(document=doc)
                 .order_by('-created_at', '-id')[kept:])
    for v in stale:
        v.delete()  # post_delete removes the blob


def listing(doc: Document) -> list[dict]:
    return [
        {
            'id': v.id,
            'created_at': v.created_at,
            'source': v.source,
            'name': v.name,
            'size': v.file_size,
            'has_spec': v.spec is not None,
        }
        for v in DocumentVersion.objects.filter(document=doc).order_by('-created_at', '-id')
    ]


def version_bytes(version: DocumentVersion) -> bytes:
    if version.file:
        with version.file.open('rb') as fh:
            return fh.read()
    return (version.content_text or '').encode('utf-8')


def restore(doc: Document, version: DocumentVersion) -> Document:
    """Put `version` back. The state it replaces becomes a version itself."""
    from . import office_edit

    if version.document_id != doc.id:
        raise office_edit.EditError('That version belongs to another file.', 404)
    # A pending office autosave is the user's latest edit: render it first,
    # so the version taken below holds it and the restore can be undone back
    # to it. Otherwise it is cleared by the overwrite and kept nowhere.
    from .drafts import ensure_rendered

    doc = ensure_rendered(doc)
    snapshot(doc, 'restore', force=True)

    if version.file:
        doc = office_edit.replace_bytes(doc, version_bytes(version),
                                        text=version.content_text, version_source=None)
    elif doc.file:
        doc = office_edit.replace_bytes(doc, version.content_text.encode('utf-8'),
                                        text=version.content_text, version_source=None)
    else:
        doc.content_text = version.content_text
        doc.file_size = len(version.content_text.encode('utf-8'))
        doc.save(update_fields=['content_text', 'file_size', 'updated_at'])

    # The spec travels with the bytes: a restored deck must be editable as the
    # deck it was, and a version with no spec (an edited workbook) must not
    # leave today's spec describing yesterday's bytes.
    metadata = dict(doc.metadata or {})
    if version.spec is not None:
        metadata['spec'] = version.spec
    else:
        metadata.pop('spec', None)
    if metadata != (doc.metadata or {}):
        doc.metadata = metadata
        doc.save(update_fields=['metadata', 'updated_at'])
    return doc
