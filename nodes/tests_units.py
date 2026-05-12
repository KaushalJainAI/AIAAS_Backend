"""
Unit tests for the nodes app — registry, base handler contract, individual
node handlers, and the custom-node loader (security-sensitive).

Pure-logic only: no DB, no real LLM/HTTP, no filesystem-backed sandbox.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

from django.test import SimpleTestCase

from nodes.handlers.base import (
    BaseNodeHandler, FieldConfig, FieldType, HandleDef, NodeCategory,
    NodeExecutionResult, NodeItem, build_json_schema_from_fields,
    format_schema_for_prompt,
)
from nodes.handlers.logic_nodes import IfNode, LoopNode, SplitInBatchesNode, StopNode
from nodes.handlers.registry import NodeRegistry
from nodes.handlers.triggers import (
    ManualTriggerNode, TelegramTriggerNode, WebhookTriggerNode,
)
from nodes.handlers.node_loader import (
    BLOCKED_IMPORTS, NodeLoader, NodeValidationError,
)


# ─────────────────────────────────────────────────────────────────────────
# Test fixtures
# ─────────────────────────────────────────────────────────────────────────

def _ctx(**overrides):
    """Build a minimal ExecutionContext-like stand-in."""
    base = dict(
        execution_id=uuid4(),
        user_id=1,
        workflow_id=1,
        current_node_id="n1",
        credentials={},
        variables={},
        node_outputs={},
        loop_stats={},
        current_input=None,
    )
    base.update(overrides)

    # SimpleNamespace gives attribute access; we add method stubs the
    # loop/batch nodes call into.
    ns = SimpleNamespace(**base)
    ns._loop_counts = {}
    ns._loop_items = {}
    ns._batch_cursors = {}
    ns._accumulated = {}
    ns.get_loop_count = lambda nid: ns._loop_counts.get(nid, 0)
    ns.set_loop_items = lambda nid, items: ns._loop_items.__setitem__(nid, items)
    ns.get_loop_items = lambda nid: ns._loop_items.get(nid, [])
    ns.set_batch_cursor = lambda nid, c: ns._batch_cursors.__setitem__(nid, c)
    ns.get_batch_cursor = lambda nid: ns._batch_cursors.get(nid, 0)
    ns.get_accumulated_results = lambda nid: ns._accumulated.get(nid, [])
    ns.add_warning = lambda msg: None
    ns.get_credential = AsyncMock(return_value=None)
    return ns


def _run(coro):
    return asyncio.run(coro)


# ─────────────────────────────────────────────────────────────────────────
# NodeExecutionResult / NodeItem
# ─────────────────────────────────────────────────────────────────────────

class NodeExecutionResultTests(SimpleTestCase):
    def test_legacy_data_kw_converted_to_items(self):
        # Backward-compat shim: passing data= should populate items[0].json.
        r = NodeExecutionResult(success=True, data={"a": 1})
        self.assertEqual(len(r.items), 1)
        self.assertEqual(r.items[0].json, {"a": 1})

    def test_data_ignored_when_items_provided(self):
        r = NodeExecutionResult(items=[NodeItem(json={"x": 1})], data={"y": 2})
        self.assertEqual(r.items[0].json, {"x": 1})

    def test_get_data_returns_first_item(self):
        r = NodeExecutionResult(items=[NodeItem(json={"k": "v"}), NodeItem(json={"k": "w"})])
        self.assertEqual(r.get_data(), {"k": "v"})

    def test_get_data_empty_returns_dict(self):
        r = NodeExecutionResult(success=True)
        self.assertEqual(r.get_data(), {})

    def test_from_data_helper(self):
        r = NodeExecutionResult.from_data({"hello": "world"})
        self.assertTrue(r.success)
        self.assertEqual(r.items[0].json, {"hello": "world"})

    def test_from_items_list_helper(self):
        r = NodeExecutionResult.from_items_list([{"a": 1}, {"a": 2}])
        self.assertEqual(len(r.items), 2)
        self.assertEqual(r.get_all_json(), [{"a": 1}, {"a": 2}])

    def test_node_item_json_alias(self):
        # n8n compat: serialize as 'json' even though field is json_data.
        item = NodeItem(json={"foo": "bar"})
        dumped = item.model_dump(by_alias=True)
        self.assertIn("json", dumped)
        self.assertEqual(dumped["json"], {"foo": "bar"})


# ─────────────────────────────────────────────────────────────────────────
# Schema helpers
# ─────────────────────────────────────────────────────────────────────────

class SchemaHelperTests(SimpleTestCase):
    def test_build_schema_strips_custom_prefix(self):
        out = build_json_schema_from_fields([
            {"id": "custom_age", "type": "number", "label": "Age"},
            {"id": "custom_name", "type": "text", "label": "Name"},
        ])
        self.assertEqual(set(out["properties"].keys()), {"age", "name"})
        self.assertEqual(out["properties"]["age"]["type"], "number")
        self.assertEqual(out["properties"]["name"]["type"], "string")
        self.assertEqual(out["additionalProperties"], False)

    def test_build_schema_empty_returns_none(self):
        self.assertIsNone(build_json_schema_from_fields([]))
        self.assertIsNone(build_json_schema_from_fields(None))

    def test_build_schema_skips_fields_without_id(self):
        out = build_json_schema_from_fields([{"id": "", "type": "text"}])
        self.assertIsNone(out)

    def test_format_schema_includes_no_extra_text_warning(self):
        schema = {"properties": {"name": {"type": "string"}}}
        prompt = format_schema_for_prompt(schema)
        self.assertIn('"name"', prompt)
        self.assertIn("no extra text", prompt)
        self.assertNotIn("```", prompt)  # We instruct *no* fences.


# ─────────────────────────────────────────────────────────────────────────
# Registry (singleton, isolation-friendly via clear())
# ─────────────────────────────────────────────────────────────────────────

class _DummyHandler(BaseNodeHandler):
    node_type = "dummy_test"
    name = "Dummy"
    category = NodeCategory.UTILITY.value
    fields: list = []

    async def execute(self, input_data, config, context):
        return NodeExecutionResult(success=True)


class _NoTypeHandler(BaseNodeHandler):
    node_type = ""
    name = "No Type"
    async def execute(self, input_data, config, context):
        return NodeExecutionResult(success=True)


class RegistryTests(SimpleTestCase):
    def setUp(self):
        # Use a dedicated registry instance state for isolation.
        self.reg = NodeRegistry.get_instance()

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

    def test_contains_operator(self):
        self.reg.register(_DummyHandler)
        try:
            self.assertIn("dummy_test", self.reg)
        finally:
            self.reg.unregister("dummy_test")


# ─────────────────────────────────────────────────────────────────────────
# BaseNodeHandler.validate_config
# ─────────────────────────────────────────────────────────────────────────

class ValidateConfigTests(SimpleTestCase):
    def test_required_field_missing_reports_error(self):
        class H(BaseNodeHandler):
            node_type = "h"
            name = "H"
            fields = [FieldConfig(name="x", label="X", field_type=FieldType.STRING, required=True)]
            async def execute(self, *a, **k): return NodeExecutionResult(success=True)
        errs = H().validate_config({})
        self.assertEqual(len(errs), 1)
        self.assertIn("'X'", errs[0])

    def test_select_invalid_option_reports(self):
        class H(BaseNodeHandler):
            node_type = "h"
            name = "H"
            fields = [FieldConfig(
                name="op", label="Op", field_type=FieldType.SELECT,
                options=["a", "b"], required=False, default="a",
            )]
            async def execute(self, *a, **k): return NodeExecutionResult(success=True)
        errs = H().validate_config({"op": "c"})
        self.assertTrue(any("Invalid option" in e for e in errs))


# ─────────────────────────────────────────────────────────────────────────
# IfNode — branch routing
# ─────────────────────────────────────────────────────────────────────────

class IfNodeTests(SimpleTestCase):
    def test_equals_routes_true(self):
        r = _run(IfNode().execute(
            input_data={"json": {"status": "ok"}},
            config={"field": "status", "operator": "equals", "value": "ok"},
            context=_ctx(),
        ))
        self.assertEqual(r.output_handle, "true")

    def test_not_equals_routes_false(self):
        r = _run(IfNode().execute(
            input_data={"json": {"status": "ok"}},
            config={"field": "status", "operator": "equals", "value": "fail"},
            context=_ctx(),
        ))
        self.assertEqual(r.output_handle, "false")

    def test_dot_path_resolution(self):
        r = _run(IfNode().execute(
            input_data={"json": {"a": {"b": 5}}},
            config={"field": "a.b", "operator": "greater_than", "value": "3"},
            context=_ctx(),
        ))
        self.assertEqual(r.output_handle, "true")

    def test_missing_path_safe(self):
        # Dotted path into a non-existent key should NOT raise; falls to false.
        r = _run(IfNode().execute(
            input_data={"json": {}},
            config={"field": "missing.deep.key", "operator": "equals", "value": "x"},
            context=_ctx(),
        ))
        self.assertEqual(r.output_handle, "false")

    def test_greater_than_handles_non_numeric(self):
        ok = IfNode._eval_condition("notanumber", "greater_than", "5")
        self.assertFalse(ok)  # graceful, no exception

    def test_is_empty_variants(self):
        for v in (None, "", []):
            self.assertTrue(IfNode._eval_condition(v, "is_empty", ""))
        self.assertTrue(IfNode._eval_condition("hi", "is_not_empty", ""))


# ─────────────────────────────────────────────────────────────────────────
# LoopNode / SplitInBatchesNode — iteration semantics
# ─────────────────────────────────────────────────────────────────────────

class LoopNodeTests(SimpleTestCase):
    def test_first_iteration_emits_loop_handle(self):
        ctx = _ctx()
        r = _run(LoopNode().execute(
            input_data={"items": [1, 2, 3]},
            config={"max_loop_count": 10, "items_field": "items"},
            context=ctx,
        ))
        self.assertEqual(r.output_handle, "loop")
        self.assertEqual(r.items[0].json["item"], 1)

    def test_max_count_terminates(self):
        ctx = _ctx()
        ctx._loop_counts["n1"] = 5  # already at cap
        ctx._loop_items["n1"] = [1, 2, 3]
        ctx._batch_cursors["n1"] = 5
        r = _run(LoopNode().execute(
            input_data={"items": [1, 2, 3]},
            config={"max_loop_count": 5},
            context=ctx,
        ))
        self.assertEqual(r.output_handle, "done")

    def test_count_based_when_no_array_in_input(self):
        ctx = _ctx()
        r = _run(LoopNode().execute(
            input_data={"x": 1},
            config={"max_loop_count": 3},
            context=ctx,
        ))
        self.assertEqual(r.output_handle, "loop")
        self.assertEqual(r.items[0].json["index"], 0)

    def test_underscore_keys_skipped_for_autodetect(self):
        # Auto-detect should ignore keys starting with `_`.
        ctx = _ctx()
        r = _run(LoopNode().execute(
            input_data={"_internal": [1, 2], "_meta": "x"},
            config={"max_loop_count": 5},
            context=ctx,
        ))
        # No real array → count-based.
        self.assertIn("index", r.items[0].json)


class SplitInBatchesTests(SimpleTestCase):
    def test_first_batch_takes_n_items(self):
        ctx = _ctx()
        r = _run(SplitInBatchesNode().execute(
            input_data={"items": list(range(10))},
            config={"batch_size": 3, "max_loop_count": 100, "items_field": "items"},
            context=ctx,
        ))
        self.assertEqual(r.output_handle, "loop")
        self.assertEqual(r.items[0].json["batch"], [0, 1, 2])
        self.assertEqual(r.items[0].json["batch_size"], 3)
        self.assertFalse(r.items[0].json["is_last_batch"])

    def test_last_batch_flag_when_items_exhaust(self):
        ctx = _ctx()
        ctx._loop_items["n1"] = [1, 2, 3]
        ctx._batch_cursors["n1"] = 0
        ctx._loop_counts["n1"] = 0  # treat as already initialized
        r = _run(SplitInBatchesNode().execute(
            input_data={"items": [1, 2, 3]},
            config={"batch_size": 5, "max_loop_count": 100},
            context=ctx,
        ))
        self.assertTrue(r.items[0].json["is_last_batch"])


# ─────────────────────────────────────────────────────────────────────────
# Triggers
# ─────────────────────────────────────────────────────────────────────────

class TriggerTests(SimpleTestCase):
    def test_manual_trigger_outputs_metadata(self):
        r = _run(ManualTriggerNode().execute({}, {}, _ctx()))
        self.assertTrue(r.success)
        self.assertEqual(r.items[0].json["trigger_type"], "manual")

    def test_webhook_trigger_uses_test_data_when_input_empty(self):
        r = _run(WebhookTriggerNode().execute(
            input_data={},
            config={"method": "POST", "test_data": {"body": {"hello": "world"}}},
            context=_ctx(),
        ))
        self.assertEqual(r.items[0].json["body"], {"hello": "world"})

    def test_telegram_trigger_parses_command_with_botname(self):
        # /start@MyBot  →  command="start"
        payload = {"message": {
            "text": "/start@MyBot some args",
            "from": {"id": 1, "username": "u"},
            "chat": {"id": 99, "type": "private"},
        }}
        r = _run(TelegramTriggerNode().execute(
            input_data={"payload": payload},
            config={"trigger_on": "command"},
            context=_ctx(),
        ))
        self.assertEqual(r.items[0].json["command"], "start")
        self.assertEqual(r.items[0].json["args"], "some args")

    def test_telegram_trigger_handles_callback_query(self):
        payload = {"callback_query": {
            "from": {"id": 1, "username": "u"},
            "message": {"text": "hello", "chat": {"id": 5}},
        }}
        r = _run(TelegramTriggerNode().execute(
            input_data={"payload": payload},
            config={"trigger_on": "callback_query"},
            context=_ctx(),
        ))
        self.assertEqual(r.items[0].json["text"], "hello")


# ─────────────────────────────────────────────────────────────────────────
# StopNode
# ─────────────────────────────────────────────────────────────────────────

class StopNodeTests(SimpleTestCase):
    def test_stop_returns_no_output_handle(self):
        # Terminal node — output_handle=None signals no further routing.
        r = _run(StopNode().execute({}, {"message": "fin"}, _ctx()))
        self.assertTrue(r.success)
        self.assertIsNone(r.output_handle)
        self.assertEqual(r.items[0].json["status"], "stopped")


# ─────────────────────────────────────────────────────────────────────────
# Custom-node loader (SECURITY)
# ─────────────────────────────────────────────────────────────────────────

class NodeLoaderSecurityTests(SimpleTestCase):
    """
    The node loader is the primary attack surface for user-supplied code.
    These tests verify that the AST screen blocks dangerous primitives.
    """

    def setUp(self):
        self.loader = NodeLoader()

    def test_blocks_os_import(self):
        errs = self.loader.validate_code("import os\n")
        self.assertTrue(any("os" in e for e in errs))

    def test_blocks_subprocess_import(self):
        errs = self.loader.validate_code("import subprocess\n")
        self.assertTrue(any("subprocess" in e for e in errs))

    def test_blocks_from_socket_import(self):
        errs = self.loader.validate_code("from socket import socket\n")
        self.assertTrue(any("socket" in e for e in errs))

    def test_blocks_eval_call(self):
        code = "x = eval('1+1')\nclass C(BaseNodeHandler): pass\n"
        errs = self.loader.validate_code(code)
        self.assertTrue(any("eval" in e for e in errs))

    def test_blocks_dunder_import(self):
        code = "x = __import__('os')\nclass C(BaseNodeHandler): pass\n"
        errs = self.loader.validate_code(code)
        self.assertTrue(any("__import__" in e for e in errs))

    def test_warns_on_subprocess_methods(self):
        code = "x.system('rm -rf /')\nclass C(BaseNodeHandler): pass\n"
        errs = self.loader.validate_code(code)
        self.assertTrue(any("system" in e for e in errs))

    def test_requires_basenodehandler_subclass(self):
        errs = self.loader.validate_code("x = 1\n")
        self.assertTrue(any("BaseNodeHandler" in e for e in errs))

    def test_syntax_error_short_circuits(self):
        errs = self.loader.validate_code("def broken(:\n")
        self.assertEqual(len(errs), 1)
        self.assertIn("Syntax error", errs[0])

    def test_load_from_code_rejects_blocked_imports(self):
        with self.assertRaises(NodeValidationError):
            self.loader.load_from_code(
                "import os\nclass C(BaseNodeHandler): pass\n", "x"
            )

    def test_load_from_code_rejects_non_async_execute(self):
        code = (
            "class C(BaseNodeHandler):\n"
            "    node_type = 'custom_x'\n"
            "    name = 'X'\n"
            "    category = 'utility'\n"
            "    def execute(self, input_data, config, context):\n"
            "        return None\n"
        )
        with self.assertRaises(NodeValidationError):
            self.loader.load_from_code(code, "x")

    def test_load_from_code_rejects_non_custom_prefix(self):
        # Custom nodes must start with 'custom_' to avoid colliding with built-ins.
        code = (
            "class C(BaseNodeHandler):\n"
            "    node_type = 'http_request'\n"
            "    name = 'X'\n"
            "    category = 'utility'\n"
            "    async def execute(self, input_data, config, context):\n"
            "        return None\n"
        )
        with self.assertRaises(NodeValidationError):
            self.loader.load_from_code(code, "x")

    def test_load_from_code_accepts_valid(self):
        code = (
            "class C(BaseNodeHandler):\n"
            "    node_type = 'custom_ok'\n"
            "    name = 'OK'\n"
            "    category = 'utility'\n"
            "    async def execute(self, input_data, config, context):\n"
            "        return None\n"
        )
        cls = self.loader.load_from_code(code, "modx")
        self.assertEqual(cls.node_type, "custom_ok")

    def test_blocked_imports_set_covers_critical_modules(self):
        # Sanity: don't accidentally remove these from the deny-list.
        for mod in ("os", "subprocess", "ctypes", "socket"):
            self.assertIn(mod, BLOCKED_IMPORTS)
