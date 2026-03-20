"""
Node Registry Singleton

Central registry for all available node handlers.
Provides node discovery and schema generation for frontend.
"""
import logging
from typing import Type
from nodes.handlers.base import BaseNodeHandler, NodeSchema

logger = logging.getLogger(__name__)


class NodeRegistry:
    """
    Singleton registry for node handlers.
    
    Usage:
        registry = NodeRegistry.get_instance()
        registry.register(HTTPRequestNode)
        handler = registry.get_handler('http_request')
    """
    
    _instance: 'NodeRegistry | None' = None
    _handlers: dict[str, Type[BaseNodeHandler]] = {}
    
    def __new__(cls) -> 'NodeRegistry':
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance
    
    @classmethod
    def get_instance(cls) -> 'NodeRegistry':
        """Get the singleton instance"""
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance
    
    def register(self, handler_class: Type[BaseNodeHandler]) -> None:
        """
        Register a node handler class.
        
        Args:
            handler_class: Subclass of BaseNodeHandler to register
        """
        node_type = handler_class.node_type
        if not node_type:
            raise ValueError(f"Handler {handler_class.__name__} must define node_type")
        
        self._handlers[node_type] = handler_class
    
    def unregister(self, node_type: str) -> None:
        """Remove a handler from registry"""
        self._handlers.pop(node_type, None)
    
    def get_handler(self, node_type: str) -> BaseNodeHandler:
        """
        Get an instance of a handler by node type.
        
        Args:
            node_type: The unique node type identifier
        
        Returns:
            Instance of the handler
            
        Raises:
            KeyError: If node type is not registered
        """
        if node_type not in self._handlers:
            raise KeyError(f"Unknown node type: {node_type}")
        
        return self._handlers[node_type]()
    
    def get_handler_class(self, node_type: str) -> Type[BaseNodeHandler]:
        """Get the handler class (not instance)"""
        if node_type not in self._handlers:
            raise KeyError(f"Unknown node type: {node_type}")
        
        return self._handlers[node_type]
    
    def has_handler(self, node_type: str) -> bool:
        """Check if a handler is registered"""
        return node_type in self._handlers
    
    def get_all_schemas(self) -> list[dict]:
        """
        Get schemas for all registered nodes.
        
        Returns:
            List of node schemas as dicts (for JSON serialization)
        """
        schemas = []
        for handler_class in self._handlers.values():
            handler = handler_class()
            schemas.append(handler.get_schema().model_dump(by_alias=True))
        return schemas
    
    def get_schemas_by_category(self) -> dict[str, list[dict]]:
        """
        Get schemas grouped by category.
        
        Returns:
            Dict mapping category -> list of node schemas
        """
        grouped: dict[str, list[dict]] = {}
        
        for handler_class in self._handlers.values():
            handler = handler_class()
            schema = handler.get_schema().model_dump(by_alias=True)
            category = schema['category']
            
            if category not in grouped:
                grouped[category] = []
            grouped[category].append(schema)
        
        return grouped
    
    def get_node_types(self) -> list[str]:
        """Get list of all registered node types"""
        return list(self._handlers.keys())
    
    def clear(self) -> None:
        """Clear all registered handlers (for testing)"""
        self._handlers.clear()
    
    def __len__(self) -> int:
        return len(self._handlers)
    
    def __contains__(self, node_type: str) -> bool:
        return node_type in self._handlers


# Global convenience function
def get_registry() -> NodeRegistry:
    """Get the global NodeRegistry instance"""
    registry = NodeRegistry.get_instance()
    
    # Use absolute imports to avoid circular/ambiguous import issues in Django
    from nodes.handlers.core_nodes import CodeNode, SetNode
    from nodes.handlers.logic_nodes import LoopNode, SplitInBatchesNode, IfNode, StopNode
    from nodes.handlers.utility_nodes import NotificationNode, SendNotificationNode
    from nodes.handlers.subworkflow_node import SubworkflowNodeHandler
    from nodes.handlers.integration_nodes import (
        GmailNode, SlackNode, GoogleSheetsNode, DiscordNode, NotionNode,
        AirtableNode, TelegramNode, TrelloNode, GitHubNode, HTTPRequestNode,
        FirecrawlScrapeNode
    )
    from nodes.handlers.triggers import (
        ManualTriggerNode, WebhookTriggerNode, ScheduleTriggerNode, EmailTriggerNode,
        FormTriggerNode, SlackTriggerNode, GoogleSheetsTriggerNode, GitHubTriggerNode,
        DiscordTriggerNode, TelegramTriggerNode, RssFeedTriggerNode, FileTriggerNode,
        SQSTriggerNode
    )
    
    # Check if we need to register nodes (checking one is enough to know if we initialized)
    if not registry.has_handler('code'):
        # Register Core
        registry.register(CodeNode)
        registry.register(SetNode)
        registry.register(IfNode)
        
        # Register Logic
        registry.register(LoopNode)
        registry.register(SplitInBatchesNode)
        registry.register(SubworkflowNodeHandler)
        
        # Register Utility
        registry.register(StopNode)
        registry.register(NotificationNode)
        registry.register(SendNotificationNode)
        
        # Register MCP (Optional)
        try:
            from mcp_integration.nodes import MCPToolNode
            registry.register(MCPToolNode)
        except (ImportError, ModuleNotFoundError) as e:
            logger.warning(f"Could not register MCPToolNode: {e}")

        # Register Native Tool Nodes (Search, Lookup, Weather, etc.)
        try:
            from nodes.handlers.langchain_nodes import (
                WikipediaNode, DuckDuckGoNode, ArxivNode,
                SerpApiNode, OpenWeatherMapNode, WolframAlphaNode, BingSearchNode,
                TavilySearchNode
            )
            registry.register(WikipediaNode)
            registry.register(DuckDuckGoNode)
            registry.register(ArxivNode)
            registry.register(SerpApiNode)
            registry.register(OpenWeatherMapNode)
            registry.register(WolframAlphaNode)
            registry.register(BingSearchNode)
            registry.register(TavilySearchNode)
        except (ImportError, ModuleNotFoundError) as e:
            logger.warning(f"Could not register LangChain nodes: {e}")
        
        # Register Integrations
        registry.register(GmailNode)
        registry.register(SlackNode)
        registry.register(GoogleSheetsNode)
        registry.register(DiscordNode)
        registry.register(NotionNode)
        registry.register(AirtableNode)
        registry.register(TelegramNode)
        registry.register(TrelloNode)
        registry.register(GitHubNode)
        registry.register(HTTPRequestNode)
        registry.register(FirecrawlScrapeNode)
        
        # Register AI / LLM Nodes
        from nodes.handlers.llm_nodes import OpenAINode, GeminiNode, OllamaNode, PerplexityNode, OpenRouterNode, HuggingFaceNode, XAINode
        registry.register(OpenAINode)
        registry.register(GeminiNode)
        registry.register(OllamaNode)
        registry.register(PerplexityNode)
        registry.register(OpenRouterNode)
        registry.register(HuggingFaceNode)
        registry.register(XAINode)
        
        # Register Triggers
        registry.register(ManualTriggerNode)
        registry.register(WebhookTriggerNode)
        registry.register(ScheduleTriggerNode)
        registry.register(EmailTriggerNode)
        registry.register(FormTriggerNode)
        registry.register(SlackTriggerNode)
        registry.register(GoogleSheetsTriggerNode)
        registry.register(GitHubTriggerNode)
        registry.register(DiscordTriggerNode)
        registry.register(TelegramTriggerNode)
        registry.register(RssFeedTriggerNode)
        registry.register(FileTriggerNode)
        registry.register(SQSTriggerNode)
        
    return registry
