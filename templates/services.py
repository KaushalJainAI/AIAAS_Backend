import logging
import numpy as np
from typing import List, Dict, Any

from .models import WorkflowTemplate
from inference.engine import get_platform_knowledge_base

logger = logging.getLogger(__name__)

class TemplateService:
    def publish_workflow_as_template(self, workflow_id: int) -> WorkflowTemplate | None:
        """
        Create or update a template from a workflow.
        """
        # Circular import check
        from orchestrator.models import Workflow
        
        try:
            wf = Workflow.objects.get(id=workflow_id)
            
            # Check if strictly personal or secret?
            # User said "The respective workflow should be publicly available"
            # We'll create a template with 'production' status if it's successful?
            # Or 'draft' initially? Let's say 'production' for immediate sharing as per request.
            
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
                'success_rate': wf.success_rate
            }
            
            template, created = WorkflowTemplate.objects.update_or_create(
                source_workflow_id=wf.id,
                defaults=defaults
            )
            
            # Embed for search
            import asyncio
            asyncio.run(self.update_embedding(template))
            
            return template
            
        except Workflow.DoesNotExist:
            logger.error(f"Workflow {workflow_id} not found")
            return None
        except Exception as e:
            logger.exception(f"Error publishing template: {e}")
            return None

    async def update_embedding(self, template: WorkflowTemplate):
        """Update the vector embedding for semantic search."""
        kb = get_platform_knowledge_base()
        
        text = f"{template.name}\n{template.description}\n{template.category}\n" + " ".join(template.tags)
        embedding = await kb.embed_text(text)
        
        if embedding is not None:
             template.embedding = embedding.tobytes()
             await template.asave()

    async def search_templates(self, query: str, limit: int = 10, min_score: float = 0.4) -> List[Dict[str, Any]]:
        """
        Search templates semantically.
        """
        kb = get_platform_knowledge_base()
        query_emb = await kb.embed_text(query)
        
        if query_emb is None:
            return []
            
        templates = []
        async for tmpl in WorkflowTemplate.objects.filter(status='production').exclude(embedding=None):
            templates.append(tmpl)
            
        results = []
        for tmpl in templates:
             if not tmpl.embedding:
                 continue
             try:
                 tmpl_emb = np.frombuffer(tmpl.embedding, dtype='float32')
                 norm_product = np.linalg.norm(tmpl_emb) * np.linalg.norm(query_emb)
                 score = 0 if norm_product == 0 else float(np.dot(tmpl_emb, query_emb) / norm_product)
                 
                 if score >= min_score:
                     results.append({
                         "template": tmpl,
                         "score": score
                     })
             except Exception:
                 pass
                 
        results.sort(key=lambda x: x['score'], reverse=True)
        return results[:limit]

    def _scrub_credentials(self, nodes: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        Deep copy nodes and remove sensitive credential fields.
        """
        import copy
        safe_nodes = copy.deepcopy(nodes)
        
        for node in safe_nodes:
            data = node.get('data', {})
            
            # Remove direct credential references
            if 'credential_id' in data:
                # Store it as 'required_credential_type' perhaps?
                # Or just nullify it so user is prompted to select their own.
                data['credential_id'] = None
                
            # Check for config dictionary which might contain secrets
            config = data.get('config', {})
            if isinstance(config, dict):
                 # Heuristic: Remove keys with 'api_key', 'token', 'secret', 'password'
                 keys_to_remove = []
                 for k in config.keys():
                     if any(secret in k.lower() for secret in ['api_key', 'token', 'secret', 'password', 'key']):
                         keys_to_remove.append(k)
                 
                 for k in keys_to_remove:
                     config[k] = ""
                     
            # Specifically for LLM nodes or common integrations, 
            # ensure 'credential' field is cleared if present in config
            if 'credential' in config:
                config['credential'] = None
                
        return safe_nodes
