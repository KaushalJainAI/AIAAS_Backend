import logging
from typing import Any, Dict, Optional
from difflib import SequenceMatcher

from .models import Skill
from inference.engine import get_skills_knowledge_base, run_kb_async

logger = logging.getLogger(__name__)


class SkillService:
    async def _kb_search_ids(self, query: str, top_k: int) -> list[int]:
        """Vector half only — runs on the shared KB loop, never touches ORM.

        ORM access stays on the request thread: the bridge loop has its own DB
        connection, and cross-connection reads collide with SQLite write
        transactions.
        """
        kb = get_skills_knowledge_base()
        hits = await kb.search(query, top_k=top_k)
        return [h.metadata['skill_id'] for h in hits if 'skill_id' in h.metadata]

    async def update_embedding(self, skill: Skill):
        """(Re)index a skill into the shared skills KB.

        The vector half of search is owned by `inference.engine` (FAISS HNSW);
        the `Skill` table remains the source of truth and the index a
        retrieval projection of it.
        """
        kb = get_skills_knowledge_base()
        if await kb.has_document(skill.id):
            await kb.delete_document(skill.id)

        text = f"{skill.title}\n{skill.description}\n{skill.category}\n{skill.content}"
        await kb.add_document(
            skill.id,
            text,
            metadata={
                'skill_id': skill.id,
                'user_id': skill.user_id,
                'category': skill.category,
            },
        )

    async def remove_embedding(self, skill: Skill):
        """Drop a skill's chunks from the index (deletion path)."""
        try:
            kb = get_skills_knowledge_base()
            await kb.delete_document(skill.id)
        except Exception:
            # A stale index entry is harmless — search filters by ORM rows — so
            # a failed delete only loses a rank slot, never corrupts the store.
            logger.warning(
                "[Skills] Index removal failed for skill %s", skill.pk, exc_info=True
            )

    def hybrid_search(
        self,
        query: str,
        user: Any,
        category: Optional[str] = None,
        page: int = 1,
        page_size: int = 12,
        tab: str = 'mine',
    ) -> Dict[str, Any]:
        """
        Hybrid search: FAISS ANN over the skills KB, fuzzy-text fallback when
        the embedder is unavailable (local dev routinely runs with none
        configured, and the API-backed one fails transiently). A failed embed
        degrades the ranking instead of 500ing the search.
        """
        # 1. Base Queryset
        if tab == 'public':
            queryset = Skill.objects.filter(is_shared=True)
        else:
            queryset = Skill.objects.filter(user=user)

        if category:
            queryset = queryset.filter(category=category)

        # 2. Browse mode — no query means no vector half; sort by recency.
        if not query:
            return self._paginate(queryset, page, page_size)

        # 3. Vector retrieval. Failures fall through to fuzzy-on-ORM below.
        try:
            skill_ids = run_kb_async(
                self._kb_search_ids(query, top_k=max(5, page * page_size * 4))
            )
        except Exception:
            logger.warning(
                "Skill search embedding unavailable; ranking on fuzzy match alone",
                exc_info=True,
            )
            return self._fuzzy_ranked(queryset, query, page, page_size)

        # 4. Map candidates back to rows, preserving ANN order; ORM scoping
        #    (user / is_shared / category) is re-applied here, so a stale index
        #    entry for a deleted or unshared skill can never leak.
        rows = list(queryset.filter(pk__in=skill_ids))
        by_id = {s.id: s for s in rows}
        ordered = [by_id[sid] for sid in skill_ids if sid in by_id]

        return self._paginate_ordered(ordered, page, page_size)

    def _paginate(self, queryset, page: int, page_size: int) -> Dict[str, Any]:
        total = queryset.count()
        start = (page - 1) * page_size
        end = start + page_size
        items = list(queryset.order_by('-updated_at')[start:end])
        return {
            'items': items,
            'total': total,
            'page': page,
            'pages': (total + page_size - 1) // page_size,
        }

    def _paginate_ordered(self, items: list, page: int, page_size: int) -> Dict[str, Any]:
        total = len(items)
        start = (page - 1) * page_size
        end = start + page_size
        paginated = items[start:end]
        return {
            'items': paginated,
            'total': total,
            'page': page,
            'pages': (total + page_size - 1) // page_size,
        }

    def _fuzzy_ranked(
        self, queryset, query: str, page: int, page_size: int
    ) -> Dict[str, Any]:
        """Rank the filtered set by SequenceMatcher on title/description/category."""
        items = list(queryset)
        if not items:
            return {'items': [], 'total': 0, 'page': page, 'pages': 0}

        q_lower = query.lower()
        scored = []
        for s in items:
            text = f"{s.title} {s.description} {s.category}".lower()
            scored.append((SequenceMatcher(None, q_lower, text).ratio(), s))

        scored.sort(key=lambda x: (x[0], x[1].updated_at), reverse=True)
        return self._paginate_ordered([s for _, s in scored], page, page_size)