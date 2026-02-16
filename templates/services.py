import logging
import asyncio
import numpy as np
from typing import List, Dict, Any, Optional
from django.db.models import Avg, Count, Q, F
from django.db import transaction, models
from django.conf import settings

from .models import WorkflowTemplate, WorkflowRating
from inference.engine import get_platform_knowledge_base

logger = logging.getLogger(__name__)

class TemplateService:
    async def publish_workflow_as_template(self, workflow_id: int) -> WorkflowTemplate | None:
        """
        Create or update a template from a workflow.
        """
        from orchestrator.models import Workflow
        from asgiref.sync import sync_to_async
        
        try:
            # Use sync_to_async for DB lookups if not using aget
            wf = await Workflow.objects.select_related('user').aget(id=workflow_id)

            
            # Scrub sensitive data (credentials) from nodes
            scrubbed_nodes = self._scrub_credentials(wf.nodes)

            defaults = {
                'name': wf.name,
                'description': wf.description,
                'nodes': scrubbed_nodes,
                'edges': wf.edges,
                'workflow_settings': wf.workflow_settings,
                'tags': wf.tags,
                'status': 'production', # Auto-publish
                'success_rate': wf.success_rate,
                'author': wf.user,
                'author_name': wf.user.get_full_name() or wf.user.username
            }
            
            # update_or_create doesn't have an async version in standard Django yet, 
            # so we use a custom logical implementation with aget/asave
            try:
                template = await WorkflowTemplate.objects.aget(source_workflow_id=wf.id)
                for key, value in defaults.items():
                    setattr(template, key, value)
                await template.asave()
            except WorkflowTemplate.DoesNotExist:
                template = WorkflowTemplate(source_workflow_id=wf.id, **defaults)
                await template.asave()
            
            # Embed for search
            try:
                await self.update_embedding(template)
            except Exception as e:
                logger.error(f"Failed to update embedding: {e}")
            
            return template
            
        except Workflow.DoesNotExist:
            logger.error(f"Workflow {workflow_id} not found")
            return None
        except Exception as e:
            logger.exception(f"Error publishing template: {e}")
            return None

    async def recalculate_rating(self, template_id: int):
        """Aggregate ratings and update WorkflowTemplate stats."""
        from asgiref.sync import sync_to_async
        
        @sync_to_async
        def get_stats():
            return WorkflowRating.objects.filter(template_id=template_id).aggregate(
                avg_stars=Avg('stars'),
                count=Count('id')
            )
            
        stats = await get_stats()
        
        await WorkflowTemplate.objects.filter(id=template_id).aupdate(
            average_rating=stats['avg_stars'] or 0.0,
            rating_count=stats['count'] or 0
        )

    async def increment_fork_count(self, template_id: int):
        """Increment fork count when a template is cloned."""
        await WorkflowTemplate.objects.filter(id=template_id).aupdate(
            fork_count=models.F('fork_count') + 1
        )

    async def update_embedding(self, template: WorkflowTemplate):
        """Update the vector embedding for semantic search."""
        kb = get_platform_knowledge_base()
        
        text = f"{template.name}\n{template.description}\n{template.category}\n" + " ".join(template.tags)
        embedding = await kb.embed_text(text)
        
        if embedding is not None:
             template.embedding = embedding.tobytes()
             await template.asave()

    async def hybrid_search(
        self, 
        query: str, 
        category: Optional[str] = None, 
        min_rating: Optional[float] = None,
        sort: str = 'relevance',
        page: int = 1,
        page_size: int = 12
    ) -> Dict[str, Any]:
        """
        Hybrid search combining fuzzy text match and vector similarity.
        """
        # 1. Base Queryset with filters
        queryset = WorkflowTemplate.objects.filter(status='production')
        
        if category:
            queryset = queryset.filter(category=category)
        if min_rating:
            queryset = queryset.filter(average_rating__gte=min_rating)
            
        # Materialize queryset into a list of objects we need for scoring
        # This is a DB hit, so we use sync_to_async if needed, or simply iterate.
        # Since we need embeddings, let's fetch them now.
        templates = []
        async for tmpl in queryset:
            templates.append(tmpl)

        if not templates:
            return {'items': [], 'scores': {}, 'total': 0, 'page': page, 'pages': 0}

        # 2. Get Vector search results if query is provided
        vector_results = {}
        query_emb = None
        if query:
            kb = get_platform_knowledge_base()
            query_emb = await kb.embed_text(query)

        # 3. Offload compute-heavy scoring to a separate thread
        def compute_scores(items, q_text, q_emb):
            from difflib import SequenceMatcher
            v_results = {}
            f_results = {}
            q_lower = q_text.lower() if q_text else ""
            
            for tmpl in items:
                # Vector Score
                if q_emb is not None and tmpl.embedding:
                    try:
                        tmpl_emb = np.frombuffer(tmpl.embedding, dtype='float32')
                        norm_product = np.linalg.norm(tmpl_emb) * np.linalg.norm(q_emb)
                        score = 0 if norm_product == 0 else float(np.dot(tmpl_emb, q_emb) / norm_product)
                        v_results[tmpl.id] = score
                    except Exception:
                        v_results[tmpl.id] = 0
                
                # Fuzzy Score
                if q_lower:
                    text_to_match = f"{tmpl.name} {tmpl.description} {' '.join(tmpl.tags)}".lower()
                    score = SequenceMatcher(None, q_lower, text_to_match).ratio()
                    f_results[tmpl.id] = score
                else:
                    f_results[tmpl.id] = 0
                    
            return v_results, f_results

        # Run scoring in thread
        vector_results, fuzzy_results = await asyncio.to_thread(compute_scores, templates, query, query_emb)

        # 4. Combine Scores
        scored_items = []
        for tmpl in templates:
            vec_score = vector_results.get(tmpl.id, 0)
            fuz_score = fuzzy_results.get(tmpl.id, 0)
            
            # Weights: 60% Vector, 40% Fuzzy
            # If no query, they all get 0 and rely on sorting/boosts
            combined_score = (vec_score * 0.6) + (fuz_score * 0.4)
            
            # Boosts
            if tmpl.is_featured:
                combined_score += 0.1
            
            scored_items.append({
                'id': tmpl.id,
                'instance': tmpl,
                'score': combined_score
            })

        # 5. Sorting
        if sort == 'rating':
            scored_items.sort(key=lambda x: (x['instance'].average_rating, x['score']), reverse=True)
        elif sort == 'trending':
            scored_items.sort(key=lambda x: (x['instance'].usage_count * 2 + x['instance'].rating_count), reverse=True)
        elif sort == 'newest':
            scored_items.sort(key=lambda x: x['instance'].created_at, reverse=True)
        else: # relevance
            scored_items.sort(key=lambda x: x['score'], reverse=True)

        # 6. Pagination
        total = len(scored_items)
        start = (page - 1) * page_size
        end = start + page_size
        paginated_data = scored_items[start:end]
        
        return {
            'items': [item['instance'] for item in paginated_data],
            'scores': {item['id']: item['score'] for item in paginated_data},
            'total': total,
            'page': page,
            'pages': (total + page_size - 1) // page_size
        }

    def _scrub_credentials(self, nodes: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        Deep copy nodes and remove sensitive credential fields.
        """
        import copy
        safe_nodes = copy.deepcopy(nodes)
        
        for node in safe_nodes:
            data = node.get('data', {})
            
            if 'credential_id' in data:
                data['credential_id'] = None
                
            config = data.get('config', {})
            if isinstance(config, dict):
                 keys_to_remove = []
                 for k in config.keys():
                     if any(secret in k.lower() for secret in ['api_key', 'token', 'secret', 'password', 'key']):
                         keys_to_remove.append(k)
                 
                 for k in keys_to_remove:
                     config[k] = ""
                     
            if 'credential' in config:
                config['credential'] = None
                
        return safe_nodes
