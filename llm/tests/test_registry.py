"""
Tests for the provider registry — the live surface that survived the
`nodes` app retirement.

The registry used to be a node-palette catalogue with schema serialization;
those methods went with the canvas. What remains is dispatch: register /
get_handler / has_handler, plus the four supported providers registered by
`get_registry()`.
"""
from __future__ import annotations

from django.test import SimpleTestCase

from llm.handlers.base import BaseNodeHandler, NodeExecutionResult
from llm.handlers.registry import ProviderRegistry, get_registry


class _DummyHandler(BaseNodeHandler):
    node_type = "dummy_test"
    name = "Dummy"

    async def execute(self, input_data, config, context):
        return NodeExecutionResult(success=True)


class _NoTypeHandler(BaseNodeHandler):
    node_type = ""
    name = "No Type"

    async def execute(self, input_data, config, context):
        return NodeExecutionResult(success=True)


class RegistryTests(SimpleTestCase):
    def setUp(self):
        self.reg = ProviderRegistry.get_instance()

    def test_register_then_retrieve(self):
        self.reg.register(_DummyHandler)
        try:
            self.assertTrue(self.reg.has_handler("dummy_test"))
            self.assertIsInstance(self.reg.get_handler("dummy_test"), _DummyHandler)
        finally:
            self.reg.unregister("dummy_test")

    def test_register_blank_node_type_raises(self):
        with self.assertRaises(ValueError):
            self.reg.register(_NoTypeHandler)

    def test_get_unknown_raises_keyerror(self):
        with self.assertRaises(KeyError):
            self.reg.get_handler("__nope__")

    def test_unregister_idempotent(self):
        # Should not raise if not present.
        self.reg.unregister("__never_registered__")


class RegistryBootstrapTests(SimpleTestCase):
    """`get_registry()` registers exactly the supported provider set."""

    def test_supported_providers_are_registered(self):
        from llm.providers import SUPPORTED_PROVIDERS

        registry = get_registry()
        for slug in SUPPORTED_PROVIDERS:
            self.assertTrue(registry.has_handler(slug), f"missing handler for {slug}")
            handler = registry.get_handler(slug)
            self.assertEqual(handler.node_type, slug)

    def test_bootstrap_is_idempotent(self):
        first = get_registry()
        second = get_registry()
        self.assertIs(first, second)