"""
Node Registry Singleton

Central registry for all available node handlers.
Provides node discovery and schema generation for frontend.
"""
from typing import Type
from .base import BaseNodeHandler, NodeSchema


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
            schemas.append(handler.get_schema().model_dump())
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
            schema = handler.get_schema().model_dump()
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
    
    # Lazy registration of core nodes to avoid circular imports?
    # Or just register them here if they aren't auto-discovered.
    # Assuming we need to manually register if not done elsewhere.
    # Checking existing code flow... 
    # Usually registration happens at app startup or module import.
    # Let's import and register here to be safe and ensure they exist.
    
    from .core_nodes import HTTPRequestNode, CodeNode, SetNode, IfNode
    from .logic_nodes import LoopNode, SplitInBatchesNode
    
    if not registry.has_handler('http_request'):
        registry.register(HTTPRequestNode)
        registry.register(CodeNode)
        registry.register(SetNode)
        registry.register(IfNode)
        registry.register(LoopNode)
        registry.register(SplitInBatchesNode)
        
    return registry
