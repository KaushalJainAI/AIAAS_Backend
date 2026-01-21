from django.apps import AppConfig


class NodesConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'nodes'
    
    def ready(self):
        """Register built-in nodes on app startup"""
        from .handlers.registry import get_registry
        from .handlers.triggers import (
            ManualTriggerNode,
            WebhookTriggerNode,
            ScheduleTriggerNode,
        )
        from .handlers.core_nodes import (
            HTTPRequestNode,
            CodeNode,
            SetNode,
            IfNode,
        )
        from .handlers.llm_nodes import (
            OpenAINode,
            GeminiNode,
            OllamaNode,
        )
        from .handlers.integration_nodes import (
            GmailNode,
            SlackNode,
            GoogleSheetsNode,
        )
        
        registry = get_registry()
        
        # Register trigger nodes
        registry.register(ManualTriggerNode)
        registry.register(WebhookTriggerNode)
        registry.register(ScheduleTriggerNode)
        
        # Register core nodes
        registry.register(HTTPRequestNode)
        registry.register(CodeNode)
        registry.register(SetNode)
        registry.register(IfNode)
        
        # Register LLM nodes
        registry.register(OpenAINode)
        registry.register(GeminiNode)
        registry.register(OllamaNode)
        
        # Register integration nodes
        registry.register(GmailNode)
        registry.register(SlackNode)
        registry.register(GoogleSheetsNode)

