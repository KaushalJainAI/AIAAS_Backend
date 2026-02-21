import logging
import asyncio
import numpy as np
from typing import List, Dict, Any, Optional
from django.db import models
from django.conf import settings
from difflib import SequenceMatcher

from .models import Skill
from inference.engine import get_platform_knowledge_base

logger = logging.getLogger(__name__)

class SkillService:
    async def update_embedding(self, skill: Skill):
        """Update the vector embedding for semantic search."""
        kb = get_platform_knowledge_base()
        
        text = f"{skill.title}\n{skill.description}\n{skill.category}\n{skill.content}"
        embedding = await kb.embed_text(text)
        
        if embedding is not None:
             skill.embedding = embedding.tobytes()
             await skill.asave()

    async def hybrid_search(
        self, 
        query: str, 
        user: Any,
        category: Optional[str] = None, 
        page: int = 1,
        page_size: int = 12,
        tab: str = 'mine'
    ) -> Dict[str, Any]:
        """
        Hybrid search combining fuzzy text match and vector similarity.
        """
        # 1. Base Queryset
        if tab == 'public':
            queryset = Skill.objects.filter(is_shared=True)
        else:
            queryset = Skill.objects.filter(user=user)
            
        if category:
            queryset = queryset.filter(category=category)
            
        # Materialize queryset
        skills = []
        async for s in queryset:
            skills.append(s)

        if not skills:
            return {'items': [], 'total': 0, 'page': page, 'pages': 0}

        # 2. Get Vector search results if query is provided
        vector_results = {}
        query_emb = None
        if query:
            kb = get_platform_knowledge_base()
            query_emb = await kb.embed_text(query)

        # 3. Scoring
        def compute_scores(items, q_text, q_emb):
            v_results = {}
            f_results = {}
            q_lower = q_text.lower() if q_text else ""
            
            for s in items:
                # Vector Score
                if q_emb is not None and s.embedding:
                    try:
                        s_emb = np.frombuffer(s.embedding, dtype='float32')
                        norm_product = np.linalg.norm(s_emb) * np.linalg.norm(q_emb)
                        score = 0 if norm_product == 0 else float(np.dot(s_emb, q_emb) / norm_product)
                        v_results[s.id] = score
                    except Exception:
                        v_results[s.id] = 0
                
                # Fuzzy Score
                if q_lower:
                    text_to_match = f"{s.title} {s.description} {s.category}".lower()
                    score = SequenceMatcher(None, q_lower, text_to_match).ratio()
                    f_results[s.id] = score
                else:
                    f_results[s.id] = 0
                    
            return v_results, f_results

        # Run scoring in thread to avoid blocking loop
        vector_results, fuzzy_results = await asyncio.to_thread(compute_scores, skills, query, query_emb)

        # 4. Combine Scores
        scored_items = []
        for s in skills:
            vec_score = vector_results.get(s.id, 0)
            fuz_score = fuzzy_results.get(s.id, 0)
            
            # Weights: 60% Vector, 40% Fuzzy
            # If no query, they all get 0 and rely on sorting
            combined_score = (vec_score * 0.6) + (fuz_score * 0.4)
            
            scored_items.append({
                'id': s.id,
                'instance': s,
                'score': combined_score
            })

        # 5. Sorting (default to relevance score, then updated_at)
        scored_items.sort(key=lambda x: (x['score'], x['instance'].updated_at), reverse=True)

        # 6. Pagination
        total = len(scored_items)
        start = (page - 1) * page_size
        end = start + page_size
        paginated_data = scored_items[start:end]
        
        return {
            'items': [item['instance'] for item in paginated_data],
            'total': total,
            'page': page,
            'pages': (total + page_size - 1) // page_size
        }
