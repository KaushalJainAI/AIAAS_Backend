"""
AI Workflow Generator and Modifier

Uses LLM to generate and modify workflows based on natural language descriptions.
"""
import json
import logging
from typing import Any

from nodes.handlers.registry import get_registry

logger = logging.getLogger(__name__)


# Template for workflow generation prompt
GENERATE_PROMPT = """You are an AI assistant that generates workflow definitions.
Given a natural language description, create a valid workflow JSON.

Available node types:
{node_types}

Output format:
{{
    "name": "Workflow Name",
    "description": "Description",
    "nodes": [
        {{
            "id": "unique_id",
            "type": "node_type",
            "position": {{"x": 100, "y": 100}},
            "data": {{
                "label": "Node Label",
                "config": {{}}
            }}
        }}
    ],
    "edges": [
        {{
            "id": "edge_id",
            "source": "source_node_id",
            "target": "target_node_id",
            "sourceHandle": "output",
            "targetHandle": "input"
        }}
    ]
}}

User request: {description}

Generate ONLY valid JSON, no explanation:"""


MODIFY_PROMPT = """You are an AI assistant that modifies workflow definitions.
Given an existing workflow and a modification request, update the workflow.

Current workflow:
{workflow_json}

Available node types:
{node_types}

Modification request: {modification}

Output the complete modified workflow as valid JSON only:"""


class AIWorkflowGenerator:
    """
    Generates workflows from natural language descriptions using LLM.
    
    Usage:
        generator = AIWorkflowGenerator()
        workflow = await generator.generate(
            description="Send an email when a webhook is received",
            user_id=1,
            credential_id="openai_cred"
        )
    """
    
    def __init__(self, llm_type: str = "openai"):
        self.llm_type = llm_type
        self._registry = get_registry()
    
    def _get_node_types_description(self) -> str:
        """Get a summary of available node types."""
        schemas = self._registry.get_all_schemas()
        
        descriptions = []
        for schema in schemas:
            fields_list = schema.get('fields', [])
            # fields are also dicts after model_dump
            fields_desc = ", ".join([f.get('id') for f in fields_list[:5]]) if fields_list else "none"
            descriptions.append(
                f"- {schema.get('type')}: {schema.get('name')} ({schema.get('category')}) - fields: {fields_desc}"
            )
        
        return "\n".join(descriptions)
    
    async def generate(
        self,
        description: str,
        user_id: int,
        credential_id: str | None = None,
    ) -> dict[str, Any]:
        """
        Generate a workflow from a natural language description.
        
        Args:
            description: What the workflow should do
            user_id: User making the request
            credential_id: LLM credential ID
            
        Returns:
            Generated workflow JSON
        """
        try:
            # Try to find similar templates first
            strategy, template = await self.decide_strategy(description)
            
            if strategy == "reuse" and template:
                return await self.clone_and_modify_template(template.id, description, user_id, modify=False)
            
            elif strategy == "clone" and template:
                return await self.clone_and_modify_template(template.id, description, user_id, modify=True)
            
            # Default: Create from scratch
            return await self._generate_from_scratch(description, user_id, credential_id)
            
        except Exception as e:
            logger.error(f"Failed to generate workflow: {e}")
            return {
                "error": f"Generation failed: {str(e)}",
                "raw_response": "",
            }

    async def _generate_from_scratch(self, description: str, user_id: int, credential_id: str | None) -> dict:
        """Internal method to generate from scratch."""
        node_types = self._get_node_types_description()
        prompt = GENERATE_PROMPT.format(
            node_types=node_types,
            description=description
        )
        
        response = await self._call_llm(prompt, user_id, credential_id)
        workflow = self._parse_json_response(response)
        
        if not self._validate_workflow(workflow):
            raise ValueError("Invalid workflow structure")
            
        return workflow

    async def decide_strategy(self, requirement: str) -> tuple[str, Any]:
        """Decide whether to create new, clone, or reuse."""
        from templates.services import TemplateService
        tm = TemplateService()
        
        results = await tm.search_templates(requirement, limit=1)
        if not results:
            return "create", None
            
        best = results[0]
        score = best['score']
        template = best['template']
        
        if score > 0.95:
            return "reuse", template
        elif score > 0.6:
            return "clone", template
        else:
            return "create", None

    async def clone_and_modify_template(self, template_id: int, requirement: str, user_id: int, modify: bool = True) -> dict:
        """Clone a template and optionally establish modifications."""
        from templates.models import WorkflowTemplate
        tmpl = await WorkflowTemplate.objects.aget(id=template_id)
        
        wf_json = {
            "name": f"{tmpl.name} (Copy)",
            "description": tmpl.description,
            "nodes": tmpl.nodes,
            "edges": tmpl.edges,
            "workflow_settings": tmpl.workflow_settings
        }
        
        if modify:
            return await self.modify(wf_json, requirement, user_id)
        return wf_json

    
    async def modify(
        self,
        workflow: dict[str, Any],
        modification: str,
        user_id: int,
        credential_id: str | None = None,
    ) -> dict[str, Any]:
        """
        Modify an existing workflow based on natural language request.
        
        Args:
            workflow: Current workflow JSON
            modification: What to change
            user_id: User making the request
            credential_id: LLM credential ID
            
        Returns:
            Modified workflow JSON
        """
        node_types = self._get_node_types_description()
        prompt = MODIFY_PROMPT.format(
            workflow_json=json.dumps(workflow, indent=2),
            node_types=node_types,
            modification=modification
        )
        
        response = await self._call_llm(prompt, user_id, credential_id)
        
        try:
            modified = self._parse_json_response(response)
            
            if not self._validate_workflow(modified):
                raise ValueError("Invalid workflow structure")
            
            return modified
            
        except Exception as e:
            logger.error(f"Failed to modify workflow: {e}")
            return {
                "error": str(e),
                "original": workflow,
            }
    
    async def _call_llm(
        self,
        prompt: str,
        user_id: int,
        credential_id: str | None
    ) -> str:
        """Call LLM to generate response."""
        from compiler.schemas import ExecutionContext
        from uuid import uuid4
        
        if not self._registry.has_handler(self.llm_type):
            raise ValueError(f"LLM type '{self.llm_type}' not available")
        
        handler = self._registry.get_handler(self.llm_type)
        
        context = ExecutionContext(
            execution_id=uuid4(),
            user_id=user_id,
            workflow_id=0
        )
        
        config = {
            "prompt": prompt,
            "credential": credential_id,
            "model": "gpt-4o" if self.llm_type == "openai" else "gemini-1.5-pro",
            "temperature": 0.2,
            "max_tokens": 4000,
        }
        
        result = await handler.execute({}, config, context)
        
        if result.success:
            return result.data.get("content", "")
        else:
            raise Exception(result.error)
    
    def _parse_json_response(self, response: str) -> dict:
        """Parse JSON from LLM response."""
        # Try to find JSON in the response
        response = response.strip()
        
        # Remove markdown code blocks if present
        if response.startswith("```"):
            lines = response.split("\n")
            response = "\n".join(lines[1:-1] if lines[-1] == "```" else lines[1:])
        
        # Find JSON object
        start = response.find("{")
        end = response.rfind("}") + 1
        
        if start >= 0 and end > start:
            json_str = response[start:end]
            return json.loads(json_str)
        
        raise ValueError("No valid JSON found in response")
    
    def _validate_workflow(self, workflow: dict) -> bool:
        """Validate workflow has required structure."""
        if not isinstance(workflow, dict):
            return False
        
        if "nodes" not in workflow or not isinstance(workflow["nodes"], list):
            return False
        
        if "edges" not in workflow or not isinstance(workflow["edges"], list):
            return False
        
        # Validate each node has required fields
        for node in workflow["nodes"]:
            if not all(k in node for k in ["id", "type", "position"]):
                return False
        
        # Validate each edge has required fields
        for edge in workflow["edges"]:
            if not all(k in edge for k in ["source", "target"]):
                return False
        
        return True
    
    async def suggest_improvements(
        self,
        workflow: dict[str, Any],
        user_id: int,
        credential_id: str | None = None,
    ) -> list[str]:
        """
        Suggest improvements for an existing workflow.
        
        Returns list of suggestions.
        """
        prompt = f"""Analyze this workflow and suggest 3-5 improvements:

{json.dumps(workflow, indent=2)}

Return as a JSON array of strings:
["suggestion 1", "suggestion 2", ...]"""
        
        response = await self._call_llm(prompt, user_id, credential_id)
        
        try:
            suggestions = self._parse_json_response(response)
            if isinstance(suggestions, list):
                return suggestions
            return [str(suggestions)]
        except:
            return [response]


# Global instance
_generator: AIWorkflowGenerator | None = None


def get_ai_generator() -> AIWorkflowGenerator:
    """Get global AI workflow generator."""
    global _generator
    if _generator is None:
        _generator = AIWorkflowGenerator()
    return _generator
