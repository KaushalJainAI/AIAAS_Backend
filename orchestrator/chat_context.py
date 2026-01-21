"""
AI Chat Context - Context-Aware Responses

Provides workflow and node context for AI chat interactions.
"""
import json
import logging
from typing import Any

logger = logging.getLogger(__name__)


class ChatContext:
    """
    Provides context for AI chat responses.
    
    Includes:
    - Current workflow structure
    - Selected node details
    - Execution history
    - Available node types
    """
    
    def __init__(self, user_id: int, workflow_id: int | None = None):
        self.user_id = user_id
        self.workflow_id = workflow_id
        self._workflow = None
        self._node_id: str | None = None
    
    async def load_workflow(self) -> dict | None:
        """Load workflow context."""
        if not self.workflow_id:
            return None
        
        from asgiref.sync import sync_to_async
        from orchestrator.models import Workflow
        
        @sync_to_async
        def get_workflow():
            try:
                return Workflow.objects.get(id=self.workflow_id, user_id=self.user_id)
            except Workflow.DoesNotExist:
                return None
        
        self._workflow = await get_workflow()
        return self._workflow
    
    def set_node_focus(self, node_id: str) -> None:
        """Set focus on a specific node for context."""
        self._node_id = node_id
    
    async def build_system_prompt(self) -> str:
        """Build a context-aware system prompt."""
        parts = [
            "You are an AI assistant helping users build and understand workflows.",
            "You have access to the user's workflow context and can provide specific guidance.",
        ]
        
        if self._workflow:
            parts.append(f"\nCurrent Workflow: {self._workflow.name}")
            parts.append(f"Description: {self._workflow.description or 'No description'}")
            parts.append(f"Status: {self._workflow.status}")
            parts.append(f"Number of nodes: {len(self._workflow.nodes)}")
            
            # Add node summary
            node_types = {}
            for node in self._workflow.nodes:
                node_type = node.get('type', 'unknown')
                node_types[node_type] = node_types.get(node_type, 0) + 1
            
            if node_types:
                parts.append(f"\nNode types: {', '.join(f'{k}({v})' for k, v in node_types.items())}")
            
            # Add focused node details
            if self._node_id:
                node = self._get_node(self._node_id)
                if node:
                    parts.append(f"\nFocused Node: {node.get('data', {}).get('label', self._node_id)}")
                    parts.append(f"Type: {node.get('type')}")
                    if node.get('data', {}).get('config'):
                        parts.append(f"Config: {json.dumps(node['data']['config'], indent=2)}")
        
        # Add available node types
        parts.append("\nAvailable node types:")
        parts.append(await self._get_node_types_summary())
        
        return "\n".join(parts)
    
    def _get_node(self, node_id: str) -> dict | None:
        """Get a specific node from the workflow."""
        if not self._workflow:
            return None
        
        for node in self._workflow.nodes:
            if node.get('id') == node_id:
                return node
        return None
    
    async def _get_node_types_summary(self) -> str:
        """Get summary of available node types."""
        from nodes.handlers.registry import get_registry
        
        registry = get_registry()
        schemas = registry.get_all_schemas()
        
        by_category = {}
        for schema in schemas:
            cat = schema.category
            if cat not in by_category:
                by_category[cat] = []
            by_category[cat].append(schema.name)
        
        lines = []
        for category, names in by_category.items():
            lines.append(f"  {category}: {', '.join(names[:5])}")
        
        return "\n".join(lines)
    
    async def get_execution_context(self) -> str:
        """Get recent execution history for context."""
        if not self._workflow:
            return "No workflow selected."
        
        from asgiref.sync import sync_to_async
        from logs.models import ExecutionLog
        
        @sync_to_async
        def get_executions():
            return list(
                ExecutionLog.objects.filter(
                    workflow_id=self.workflow_id,
                    user_id=self.user_id
                )
                .order_by('-created_at')[:5]
                .values('status', 'duration_ms', 'created_at', 'error_message')
            )
        
        executions = await get_executions()
        
        if not executions:
            return "No execution history available."
        
        lines = ["Recent executions:"]
        for ex in executions:
            status = ex['status']
            duration = ex['duration_ms'] or 0
            error = ex['error_message'][:50] if ex['error_message'] else ""
            lines.append(f"  - {status} ({duration}ms) {error}")
        
        return "\n".join(lines)


class ContextAwareChat:
    """
    Context-aware chat handler.
    
    Enriches user messages with workflow context before LLM call.
    
    Usage:
        chat = ContextAwareChat(user_id=1)
        response = await chat.send_message(
            message="How do I add error handling?",
            workflow_id=123,
            node_id="node_1"
        )
    """
    
    def __init__(self, user_id: int, llm_type: str = "openai"):
        self.user_id = user_id
        self.llm_type = llm_type
    
    async def send_message(
        self,
        message: str,
        workflow_id: int | None = None,
        node_id: str | None = None,
        conversation_id: str | None = None,
        credential_id: str | None = None,
    ) -> dict:
        """
        Send a context-aware chat message.
        
        Args:
            message: User's message
            workflow_id: Optional workflow context
            node_id: Optional focused node
            conversation_id: Optional conversation ID
            credential_id: LLM credential
            
        Returns:
            Dict with response and metadata
        """
        # Build context
        context = ChatContext(self.user_id, workflow_id)
        
        if workflow_id:
            await context.load_workflow()
        
        if node_id:
            context.set_node_focus(node_id)
        
        # Build system prompt
        system_prompt = await context.build_system_prompt()
        
        # Add execution context if available
        if workflow_id:
            exec_context = await context.get_execution_context()
            system_prompt += f"\n\n{exec_context}"
        
        # Get LLM response
        response = await self._call_llm(
            system_prompt=system_prompt,
            user_message=message,
            credential_id=credential_id,
        )
        
        # Save to conversation history
        await self._save_message(
            message=message,
            response=response.get('content', ''),
            workflow_id=workflow_id,
            conversation_id=conversation_id,
        )
        
        return {
            'response': response.get('content', ''),
            'conversation_id': conversation_id,
            'workflow_context': {
                'workflow_id': workflow_id,
                'node_id': node_id,
            },
            'tokens_used': response.get('tokens', 0),
        }
    
    async def _call_llm(
        self,
        system_prompt: str,
        user_message: str,
        credential_id: str | None,
    ) -> dict:
        """Call LLM with context."""
        from nodes.handlers.registry import get_registry
        from compiler.schemas import ExecutionContext
        from uuid import uuid4
        
        registry = get_registry()
        
        if not registry.has_handler(self.llm_type):
            return {'content': f"LLM '{self.llm_type}' not available"}
        
        handler = registry.get_handler(self.llm_type)
        
        context = ExecutionContext(
            execution_id=uuid4(),
            user_id=self.user_id,
            workflow_id=0,
        )
        
        config = {
            'prompt': user_message,
            'system_message': system_prompt,
            'credential': credential_id,
            'model': 'gpt-4o' if self.llm_type == 'openai' else 'gemini-1.5-pro',
            'temperature': 0.7,
        }
        
        try:
            result = await handler.execute({}, config, context)
            
            if result.success:
                return {
                    'content': result.data.get('content', ''),
                    'tokens': result.data.get('usage', {}).get('total_tokens', 0),
                }
            else:
                return {'content': f"Error: {result.error}"}
                
        except Exception as e:
            logger.exception(f"LLM call failed: {e}")
            return {'content': f"Error: {str(e)}"}
    
    async def _save_message(
        self,
        message: str,
        response: str,
        workflow_id: int | None,
        conversation_id: str | None,
    ) -> None:
        """Save messages to conversation history."""
        from asgiref.sync import sync_to_async
        from orchestrator.models import ConversationMessage
        from uuid import uuid4
        
        conv_id = conversation_id or str(uuid4())
        
        @sync_to_async
        def save():
            ConversationMessage.objects.create(
                user_id=self.user_id,
                conversation_id=conv_id,
                workflow_id=workflow_id,
                role='user',
                content=message,
            )
            ConversationMessage.objects.create(
                user_id=self.user_id,
                conversation_id=conv_id,
                workflow_id=workflow_id,
                role='assistant',
                content=response,
            )
        
        await save()


class ThoughtHistory:
    """
    Tracks AI "thoughts" during workflow execution.
    
    Records reasoning steps for debugging and transparency.
    """
    
    def __init__(self, execution_id: str):
        self.execution_id = execution_id
        self.thoughts: list[dict] = []
    
    def add_thought(
        self,
        node_id: str,
        thought: str,
        reasoning: str = "",
        data: dict | None = None,
    ) -> None:
        """Add a thought/reasoning step."""
        from datetime import datetime
        
        self.thoughts.append({
            'node_id': node_id,
            'thought': thought,
            'reasoning': reasoning,
            'data': data or {},
            'timestamp': datetime.utcnow().isoformat(),
        })
    
    def get_thoughts(self) -> list[dict]:
        """Get all thoughts in order."""
        return self.thoughts
    
    def get_thoughts_for_node(self, node_id: str) -> list[dict]:
        """Get thoughts for a specific node."""
        return [t for t in self.thoughts if t['node_id'] == node_id]
    
    def to_summary(self) -> str:
        """Generate a summary of reasoning."""
        lines = []
        for i, thought in enumerate(self.thoughts, 1):
            lines.append(f"{i}. [{thought['node_id']}] {thought['thought']}")
            if thought['reasoning']:
                lines.append(f"   Reasoning: {thought['reasoning']}")
        return "\n".join(lines)


# Global thought histories (keyed by execution_id)
_thought_histories: dict[str, ThoughtHistory] = {}


def get_thought_history(execution_id: str) -> ThoughtHistory:
    """Get or create thought history for an execution."""
    if execution_id not in _thought_histories:
        _thought_histories[execution_id] = ThoughtHistory(execution_id)
    return _thought_histories[execution_id]
