"""
The world's hidden knowledge base (E-2 of `docs/EVAL_ENVIRONMENTS_PLAN.md`).

One `KnowledgeBase` row per world version, named `.eval/s<suite>/v<version>`,
holding the world's KB fixture documents. Hidden by name, like the `/.eval/`
file tree: `list_knowledge_bases` never shows it, and outside an eval run
nothing addresses it — the agent's `kb_scope` is the only door in, and an
environment run's scope holds exactly this id.

**Why `raw`, not indexed.** An eval KB must be deterministic with no keys and
no jobs: `raw` has no search index at all, so there is nothing to build, race
or bill. The agent reads it the way the tool descriptions say to read a raw
KB — `list_documents`, then `read_document` — and the `cited(doc)` grader
checks it read the right one. A semantic question over ten policy documents
does not need embeddings; it needs the agent to actually open the file.

Documents are synced by name on every `prepare`: created, text-updated, or
removed so the row set always equals the fixtures. KB reads never change
state, so one KB serves every attempt of its world version; regenerating
drops the old version's KB outright (its cases are stale and never sweep).
"""
from __future__ import annotations

from typing import Any

#: Name prefix for every world KB. Hidden the way `/.eval/` is: listings
#: exclude it, id-addressed reads keep working. A literal, not an import —
#: nothing in `eval/` imports a sibling app at module scope — and pinned to
#: `inference.models.HIDDEN_KB_PREFIX` by a test.
KB_NAME_PREFIX = '.eval/'


def kb_name(suite_id: int, version: int) -> str:
    return f'{KB_NAME_PREFIX}s{suite_id}/v{version}'


def ensure_world_kb(user, suite, world, kb_fixtures: dict) -> dict[str, Any] | None:
    """Create/sync the world's hidden KB. Returns the prompt entry
    (`{'id', 'name', 'backend', 'doc_count'}`) or None when the world has no
    KB surface or no KB fixtures.

    Sync ORM; called from `EvalEnvironment.prepare`. `status='stored'` is the
    raw-backend terminal state — readable and browsable, never indexed — so
    syncing a fixture starts no embedding job.
    """
    from inference.models import Document, KnowledgeBase

    wanted = {}
    for doc in (kb_fixtures or {}).get('documents') or []:
        if not isinstance(doc, dict):
            continue
        name = str(doc.get('name') or '').strip().lstrip('/')[:200]
        text = str(doc.get('text') or '')
        if name:
            wanted[name] = text
    if not wanted:
        return None

    kb, _ = KnowledgeBase.objects.get_or_create(
        user=user, name=kb_name(suite.id, world.version),
        defaults={'backend': 'raw',
                  'description': f'Hidden eval corpus for suite {suite.id}.'},
    )
    if kb.backend != 'raw':
        kb.backend = 'raw'
        kb.save(update_fields=['backend', 'updated_at'])

    live = {d.name: d for d in Document.objects.filter(knowledge_base=kb)}
    for name, text in wanted.items():
        doc = live.get(name)
        if doc is None:
            Document.objects.create(
                user=user, knowledge_base=kb, name=name, folder=None,
                file='', file_type=_file_type(name),
                file_size=len(text.encode('utf-8')), content_text=text,
                status='stored', metadata={'created_by': 'eval-world'},
            )
        elif (doc.content_text or '') != text:
            doc.content_text = text
            doc.file_size = len(text.encode('utf-8'))
            doc.save(update_fields=['content_text', 'file_size', 'updated_at'])
    stale = [d.id for name, d in live.items() if name not in wanted]
    if stale:
        Document._base_manager.filter(id__in=stale).delete()
        from inference.signals import recount_kb
        recount_kb(kb.id)

    count = Document.objects.filter(knowledge_base=kb).count()
    if kb.doc_count != count:
        kb.doc_count = count
        kb.save(update_fields=['doc_count', 'updated_at'])
    return {'id': kb.id, 'name': kb.name, 'backend': 'raw', 'doc_count': count}


def doc_ids(user, kb_id: int) -> dict[str, int]:
    """`{name: id}` for the world's KB documents, for the `cited` grader."""
    from inference.models import Document

    return dict(Document.objects.filter(user=user, knowledge_base_id=kb_id)
                .values_list('name', 'id'))


def drop_world_kb(user, suite_id: int, version: int) -> None:
    """Delete one version's hidden KB and its documents. Sync ORM."""
    from inference.models import Document, KnowledgeBase

    kb = KnowledgeBase.objects.filter(
        user=user, name=kb_name(suite_id, version)).first()
    if kb is None:
        return
    Document._base_manager.filter(knowledge_base=kb).delete()
    kb.delete()


def drop_suite_kbs(user, suite_id: int) -> None:
    """Delete every hidden KB of a suite. Called on suite delete."""
    from inference.models import Document, KnowledgeBase

    for kb in KnowledgeBase.objects.filter(
            user=user, name__startswith=f'{KB_NAME_PREFIX}s{suite_id}/'):
        Document._base_manager.filter(knowledge_base=kb).delete()
        kb.delete()


def is_hidden_kb(name: str) -> bool:
    """Whether a KB name is a hidden world corpus. Listings outside `eval/`
    use `inference.models.visible_knowledge_bases`, built on the same
    prefix, so "hidden" cannot drift between call sites."""
    return str(name or '').startswith(KB_NAME_PREFIX)


def _file_type(name: str) -> str:
    from inference.utils import normalize_file_type

    try:
        return normalize_file_type(name)
    except Exception:  # noqa: BLE001 - a fixture name is never worth a 500
        return 'txt'


__all__ = ['KB_NAME_PREFIX', 'kb_name', 'ensure_world_kb', 'doc_ids',
           'drop_world_kb', 'drop_suite_kbs', 'is_hidden_kb']
