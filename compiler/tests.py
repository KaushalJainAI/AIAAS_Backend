"""
Unit tests for the workflow compiler.

These tests exercise the compiler in isolation from the database wherever
possible. They are written to be runnable via:

    python manage.py test compiler
    pytest Backend/compiler/tests.py

Tests that require a registered node handler mock the registry rather than
importing real handlers, so a broken handler in `nodes/` doesn't take the
compiler test suite down with it.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch
from uuid import uuid4

from django.test import SimpleTestCase, TestCase

from compiler.compiler import (
    WorkflowCompiler,
    WorkflowCompilationError,
    _compute_loop_body_sources,
    _find_expression_paths,
    _first_item_json,
)
from compiler.config_access import get_credential_ref, get_node_config
from compiler.node_types import CONDITIONAL_NODE_TYPES, LOOP_NODE_TYPES
from compiler.schemas import CompileError, ExecutionContext, _coerce_to_items
from compiler.validators import (
    topological_sort,
    validate_credentials,
    validate_dag,
    validate_type_compatibility,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_registry(known_types: set[str] | None = None) -> MagicMock:
    """Mock the node-handler registry with a configurable known-type set."""
    known = known_types or {
        "manual_trigger", "webhook_trigger", "code", "http_request",
        "if", "switch", "loop", "split_in_batches", "set",
    }
    reg = MagicMock()
    reg.has_handler.side_effect = lambda t: t in known
    handler = MagicMock()
    handler.validate_config.return_value = []
    reg.get_handler.return_value = handler
    return reg


def _node(id_: str, ntype: str, config: dict | None = None, label: str | None = None) -> dict:
    data: dict = {"nodeType": ntype}
    if config is not None:
        data["config"] = config
    if label:
        data["label"] = label
    return {"id": id_, "type": ntype, "data": data}


def _edge(s: str, t: str, handle: str | None = None) -> dict:
    e = {"source": s, "target": t}
    if handle is not None:
        e["sourceHandle"] = handle
    return e


# ---------------------------------------------------------------------------
# DAG validation
# ---------------------------------------------------------------------------

class ValidateDagTests(SimpleTestCase):
    def test_empty_workflow_errors(self):
        errors = validate_dag([], [])
        self.assertEqual([e.error_type for e in errors], ["empty_workflow"])

    def test_invalid_edge_source_reported(self):
        nodes = [_node("a", "manual_trigger")]
        edges = [_edge("ghost", "a")]
        errors = validate_dag(nodes, edges)
        self.assertTrue(any(e.error_type == "invalid_edge" for e in errors))

    def test_pure_cycle_rejected(self):
        nodes = [_node("a", "code"), _node("b", "code")]
        edges = [_edge("a", "b"), _edge("b", "a")]
        errors = validate_dag(nodes, edges)
        self.assertTrue(any(e.error_type == "dag_cycle" for e in errors))

    def test_loop_backedge_allowed(self):
        # A cycle whose back-edge terminates on a loop node is permitted.
        nodes = [
            _node("trigger", "manual_trigger"),
            _node("loop", "loop", {"max_loop_count": 5}),
            _node("body", "code"),
        ]
        edges = [
            _edge("trigger", "loop"),
            _edge("loop", "body", handle="loop"),
            _edge("body", "loop"),
        ]
        errors = validate_dag(nodes, edges)
        self.assertFalse(
            any(e.error_type == "dag_cycle" for e in errors),
            f"Loop back-edge should be legal; got {errors}",
        )

    def test_isolated_nodes_are_their_own_triggers(self):
        # A node with no edges has in-degree 0 → it's its own entry point.
        # The orphan check only fires for nodes in unreachable cyclic subgraphs
        # (e.g. a loop that no trigger feeds into), which DAG cycle-validation
        # generally rejects first.
        nodes = [_node("trigger", "manual_trigger"), _node("floater", "code")]
        errors = validate_dag(nodes, [])
        self.assertFalse(any(e.error_type == "orphan_node" for e in errors))

    def test_no_trigger_detected(self):
        # Every node is a target → no zero-in-degree nodes.
        nodes = [_node("a", "code"), _node("b", "code")]
        edges = [_edge("a", "b"), _edge("b", "a")]
        errors = validate_dag(nodes, edges)
        # Cycle errors block further checks — expect at least one error.
        self.assertTrue(errors)


# ---------------------------------------------------------------------------
# Credential validation — regression for the `credential_id` key bug.
# ---------------------------------------------------------------------------

class ValidateCredentialsTests(SimpleTestCase):
    def test_credential_id_key_detected(self):
        nodes = [_node("a", "http_request", {"credential_id": "cred_missing"})]
        errors = validate_credentials(nodes, user_credentials=set())
        self.assertEqual([e.error_type for e in errors], ["missing_credential"])

    def test_credentialId_key_detected(self):
        nodes = [_node("a", "http_request", {"credentialId": "cred_missing"})]
        errors = validate_credentials(nodes, user_credentials=set())
        self.assertEqual([e.error_type for e in errors], ["missing_credential"])

    def test_legacy_credential_key_detected(self):
        nodes = [_node("a", "http_request", {"credential": "cred_missing"})]
        errors = validate_credentials(nodes, user_credentials=set())
        self.assertEqual([e.error_type for e in errors], ["missing_credential"])

    def test_owned_credential_passes(self):
        nodes = [_node("a", "http_request", {"credential_id": "cred_1"})]
        errors = validate_credentials(nodes, user_credentials={"cred_1"})
        self.assertEqual(errors, [])

    def test_no_credential_no_error(self):
        nodes = [_node("a", "code", {"code": "pass"})]
        errors = validate_credentials(nodes, user_credentials=set())
        self.assertEqual(errors, [])


class ConfigAccessTests(SimpleTestCase):
    def test_get_node_config_prefers_config_key(self):
        self.assertEqual(
            get_node_config({"data": {"config": {"x": 1}, "label": "L"}}),
            {"x": 1},
        )

    def test_get_node_config_falls_back_to_data(self):
        self.assertEqual(get_node_config({"data": {"x": 1}}), {"x": 1})

    def test_get_node_config_empty(self):
        self.assertEqual(get_node_config({}), {})

    def test_credential_ref_priority(self):
        self.assertEqual(
            get_credential_ref({"credential_id": "a", "credentialId": "b"}), "a",
        )
        self.assertEqual(get_credential_ref({"credentialId": "b"}), "b")
        self.assertEqual(get_credential_ref({"credential": "c"}), "c")
        self.assertIsNone(get_credential_ref({"unrelated": "x"}))


# ---------------------------------------------------------------------------
# Topological sort
# ---------------------------------------------------------------------------

class TopologicalSortTests(SimpleTestCase):
    def test_linear_order(self):
        nodes = [_node("a", "code"), _node("b", "code"), _node("c", "code")]
        edges = [_edge("a", "b"), _edge("b", "c")]
        self.assertEqual(topological_sort(nodes, edges), ["a", "b", "c"])

    def test_preserves_input_order_for_parallel_branches(self):
        nodes = [_node("a", "code"), _node("b", "code"), _node("c", "code")]
        # a and b both feed c — a appears first in input.
        edges = [_edge("a", "c"), _edge("b", "c")]
        result = topological_sort(nodes, edges)
        self.assertEqual(result.index("a") < result.index("b"), True)
        self.assertEqual(result[-1], "c")

    def test_cycle_nodes_appended(self):
        # Loop cycle — permitted by validate_dag, must not drop nodes.
        nodes = [_node("a", "manual_trigger"), _node("loop", "loop"), _node("b", "code")]
        edges = [_edge("a", "loop"), _edge("loop", "b"), _edge("b", "loop")]
        result = topological_sort(nodes, edges)
        self.assertEqual(set(result), {"a", "loop", "b"})


# ---------------------------------------------------------------------------
# Type compatibility
# ---------------------------------------------------------------------------

class TypeCompatTests(SimpleTestCase):
    def test_unknown_types_pass(self):
        nodes = [_node("a", "foo_custom"), _node("b", "bar_custom")]
        edges = [_edge("a", "b")]
        self.assertEqual(validate_type_compatibility(nodes, edges), [])


# ---------------------------------------------------------------------------
# Expression path discovery
# ---------------------------------------------------------------------------

class FindExpressionPathsTests(SimpleTestCase):
    def test_flat(self):
        paths = _find_expression_paths({"a": "{{ $json.x }}", "b": "literal"})
        self.assertEqual(paths, [["a"]])

    def test_nested_dict_and_list(self):
        cfg = {"outer": {"arr": ["literal", "{{ $vars.n }}", "also literal"]}}
        paths = _find_expression_paths(cfg)
        self.assertEqual(paths, [["outer", "arr", 1]])

    def test_ignores_literals(self):
        self.assertEqual(_find_expression_paths({"x": 1, "y": "hi"}), [])


# ---------------------------------------------------------------------------
# Loop body source reachability
# ---------------------------------------------------------------------------

class LoopBodySourcesTests(SimpleTestCase):
    def test_only_body_return_edges_included(self):
        # start → loop → body → loop  (body's return is the only body edge)
        nodes = [
            _node("start", "manual_trigger"),
            _node("loop", "loop"),
            _node("body", "code"),
        ]
        edges = [
            _edge("start", "loop"),
            _edge("loop", "body", handle="loop"),
            _edge("body", "loop"),
        ]
        reachable = _compute_loop_body_sources(nodes, edges)
        self.assertIn("loop", reachable)
        # 'body' is reachable from 'loop', so body→loop is a body-return edge.
        self.assertIn("body", reachable["loop"])
        # 'start' is NOT reachable from 'loop', so start→loop is an initial feed.
        self.assertNotIn("start", reachable["loop"])


# ---------------------------------------------------------------------------
# Full compile pipeline — linear, conditional, multi-entry.
# ---------------------------------------------------------------------------

class CompileSmokeTests(SimpleTestCase):
    def _compile(self, nodes, edges, settings=None):
        # validate_node_configs imports get_registry lazily; patch the source.
        with patch("nodes.handlers.registry.get_registry", return_value=_make_registry()), \
             patch("compiler.compiler.get_registry", return_value=_make_registry()):
            compiler = WorkflowCompiler(
                {"nodes": nodes, "edges": edges, "settings": settings or {}},
                user_credentials=set(),
            )
            return compiler.compile()

    def test_linear_workflow_compiles(self):
        nodes = [_node("a", "manual_trigger"), _node("b", "code")]
        edges = [_edge("a", "b")]
        graph = self._compile(nodes, edges)
        self.assertIsNotNone(graph)

    def test_invalid_dag_raises(self):
        nodes = [_node("a", "code"), _node("b", "code")]
        edges = [_edge("a", "b"), _edge("b", "a")]
        with self.assertRaises(WorkflowCompilationError):
            self._compile(nodes, edges)

    def test_multi_entry_workflow_compiles(self):
        # Two triggers, both feeding a shared downstream node. The old compiler
        # silently dropped all but the last entry — this test guards the fix.
        nodes = [
            _node("t1", "manual_trigger"),
            _node("t2", "webhook_trigger"),
            _node("shared", "code"),
        ]
        edges = [_edge("t1", "shared"), _edge("t2", "shared")]
        graph = self._compile(nodes, edges)
        self.assertIsNotNone(graph)

    def test_conditional_with_dangling_handle(self):
        # Only the "true" handle is wired; "false" dangles. The router's END
        # fallback must not crash at graph construction.
        nodes = [_node("t", "manual_trigger"), _node("cond", "if"), _node("sink", "code")]
        edges = [_edge("t", "cond"), _edge("cond", "sink", handle="true")]
        graph = self._compile(nodes, edges)
        self.assertIsNotNone(graph)

    def test_missing_credential_raises(self):
        nodes = [_node("t", "manual_trigger"),
                 _node("h", "http_request", {"credential_id": "nope"})]
        edges = [_edge("t", "h")]
        with self.assertRaises(WorkflowCompilationError) as cm:
            self._compile(nodes, edges)
        self.assertTrue(
            any(e.error_type == "missing_credential" for e in cm.exception.errors),
        )


# ---------------------------------------------------------------------------
# ExecutionContext — expression resolution, item coercion.
# ---------------------------------------------------------------------------

class ExecutionContextExpressionTests(SimpleTestCase):
    def _ctx(self, **overrides) -> ExecutionContext:
        defaults = dict(
            execution_id=uuid4(),
            user_id=1,
            workflow_id=1,
        )
        defaults.update(overrides)
        return ExecutionContext(**defaults)

    def test_node_dot_ref_resolves(self):
        ctx = self._ctx(
            node_outputs={"n1": [{"json": {"greeting": "hi"}}]},
            node_label_to_id={"Alpha": "n1"},
        )
        self.assertEqual(ctx._evaluate_expression('$node["Alpha"].json.greeting'), "hi")

    def test_node_ref_unknown_label_returns_none(self):
        ctx = self._ctx()
        self.assertIsNone(ctx._evaluate_expression('$node["Unknown"].json.x'))

    def test_json_bracket_form_matches_dot_form(self):
        # Regression: `$json["x"]` used to fail because the slice was wrong.
        ctx = self._ctx(current_input=[{"json": {"x": 42}}])
        self.assertEqual(ctx._evaluate_expression("$json.x"), 42)
        self.assertEqual(ctx._evaluate_expression('$json["x"]'), 42)
        self.assertEqual(ctx._evaluate_expression("$input.x"), 42)
        self.assertEqual(ctx._evaluate_expression('$input["x"]'), 42)

    def test_json_alone_returns_first_item_json(self):
        ctx = self._ctx(current_input=[{"json": {"a": 1}}])
        self.assertEqual(ctx._evaluate_expression("$json"), {"a": 1})

    def test_vars_resolution(self):
        ctx = self._ctx(variables={"name": "Ada"})
        self.assertEqual(ctx._evaluate_expression("$vars.name"), "Ada")

    def test_event_fallback_into_body(self):
        ctx = self._ctx(node_outputs={"_input_global": {"body": {"id": 7}}})
        self.assertEqual(ctx._evaluate_expression("event.id"), 7)

    def test_interpolated_string_stringifies(self):
        ctx = self._ctx(variables={"n": 3})
        self.assertEqual(ctx._resolve_string_expression("n={{$vars.n}}"), "n=3")

    def test_whole_string_preserves_type(self):
        ctx = self._ctx(variables={"n": 3})
        self.assertEqual(ctx._resolve_string_expression("{{ $vars.n }}"), 3)

    def test_resolve_expressions_uses_paths(self):
        ctx = self._ctx(variables={"user": "bob"})
        cfg = {"url": "{{ $vars.user }}", "other": "static"}
        out = ctx.resolve_expressions(cfg, [["url"]])
        self.assertEqual(out, {"url": "bob", "other": "static"})

    def test_resolve_all_expressions_nested(self):
        ctx = self._ctx(variables={"n": 5})
        cfg = {"items": [{"val": "{{ $vars.n }}"}]}
        self.assertEqual(ctx.resolve_all_expressions(cfg), {"items": [{"val": 5}]})


class CoerceToItemsTests(SimpleTestCase):
    def test_list_of_bare_dicts(self):
        self.assertEqual(_coerce_to_items([{"a": 1}]), [{"json": {"a": 1}}])

    def test_list_of_json_wrapped(self):
        self.assertEqual(
            _coerce_to_items([{"json": {"a": 1}}]),
            [{"json": {"a": 1}}],
        )

    def test_dict_with_items_key(self):
        self.assertEqual(
            _coerce_to_items({"items": [{"json": {"a": 1}}, {"b": 2}]}),
            [{"json": {"a": 1}}, {"json": {"b": 2}}],
        )

    def test_scalar_wrapped(self):
        self.assertEqual(_coerce_to_items(42), [{"json": {"value": 42}}])


class FirstItemJsonTests(SimpleTestCase):
    def test_empty(self):
        self.assertEqual(_first_item_json([]), {})

    def test_json_wrapped(self):
        self.assertEqual(_first_item_json([{"json": {"a": 1}}]), {"a": 1})

    def test_bare_dict(self):
        self.assertEqual(_first_item_json([{"a": 1}]), {"a": 1})


# ---------------------------------------------------------------------------
# Node-type constant sanity — catches accidental drift.
# ---------------------------------------------------------------------------

class NodeTypeConstantTests(SimpleTestCase):
    def test_loop_types_subset_of_conditional(self):
        self.assertTrue(LOOP_NODE_TYPES.issubset(CONDITIONAL_NODE_TYPES))

    def test_core_conditionals_present(self):
        self.assertIn("if", CONDITIONAL_NODE_TYPES)
        self.assertIn("switch", CONDITIONAL_NODE_TYPES)
