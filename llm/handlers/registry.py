"""
Provider Handler Registry

The registry maps a provider slug to its handler class — the single route from
"which model did the user pick" to "which HTTP client runs the call". It is on
the agent hot path: `llm.access` checks `has_handler()` on every LLM call and
executes the model through the handler `get_handler()` returns.

What is registered here is now only the four LLM providers
(`llm.providers.SUPPORTED_PROVIDERS`): OpenRouter, NVIDIA, OpenAI and Ollama.
The registry used to hold every node in the workflow canvas — structural
handlers (core Code/Set, logic If/Loop/SplitInBatches/Stop, utility
notifications, subworkflow, every trigger) and tool-shaped ones (search,
lookup, weather, the REST connector pack, generic HTTP, MCP) — and serialise
their schemas for the frontend palette. The canvas went with the DAG runtime;
the schema methods went with it, because the only callers of `get_handler`
left are ones resolving a *provider slug* (`llm.access`, `inference/engine.py`).
Git holds the deleted handlers if a `chat/tools/` conversion ever wants them.
"""
from typing import Type

from .base import BaseNodeHandler


class ProviderRegistry:
    """
    Singleton registry for provider handlers.

    Usage:
        registry = ProviderRegistry.get_instance()
        registry.register(OpenRouterNode)
        handler = registry.get_handler('openrouter')
    """

    _instance: 'ProviderRegistry | None' = None
    _handlers: dict[str, Type[BaseNodeHandler]] = {}

    def __new__(cls) -> 'ProviderRegistry':
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    @classmethod
    def get_instance(cls) -> 'ProviderRegistry':
        """Get the singleton instance"""
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def register(self, handler_class: Type[BaseNodeHandler]) -> None:
        """
        Register a provider handler class.

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

    def has_handler(self, node_type: str) -> bool:
        """Check if a handler is registered"""
        return node_type in self._handlers


# Global convenience function
def get_registry() -> ProviderRegistry:
    """
    Get the global ProviderRegistry, registering the supported providers lazily.

    This function is on the agent hot path: `llm.access` calls
    `get_registry().has_handler(node_type)` on every LLM call, and then executes
    the model through the handler it returns.

    Registration is lazy: `openrouter` is the default provider, so it is the
    cheapest thing to probe for "have we initialised yet".
    """
    registry = ProviderRegistry.get_instance()

    # Use absolute imports to avoid circular/ambiguous import issues in Django.
    # The supported provider set is `llm.providers.SUPPORTED_PROVIDERS`. Three of
    # the four speak the OpenAI chat-completions protocol and are declared on a
    # shared base in llm/handlers/llm_providers.py; Ollama keeps its own
    # transport because it posts to /api/chat rather than /v1/chat/completions.
    from llm.handlers.llm_nodes import OllamaNode
    from llm.handlers.llm_providers import NvidiaNode, OpenAINode, OpenRouterNode

    if not registry.has_handler('openrouter'):
        registry.register(OpenRouterNode)
        registry.register(NvidiaNode)
        registry.register(OpenAINode)
        registry.register(OllamaNode)

    return registry