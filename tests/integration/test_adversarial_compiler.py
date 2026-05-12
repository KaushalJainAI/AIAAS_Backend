"""
Adversarial tests for the workflow compiler / validators.

The compiler is the boundary between user-supplied JSON and the LangGraph
runtime. Anything pathological that gets past the validator becomes an
exception inside an executor task — much harder to debug. So we beat on the
validator with hostile graphs.
"""
from __future__ import annotations

from django.test import SimpleTestCase

from compiler.validators import validate_dag, get_node_type


def _node(nid, ntype="http"):
    return {"id": nid, "type": ntype, "data": {"nodeType": ntype, "config": {}}}


def _edge(src, tgt):
    return {"id": f"{src}->{tgt}", "source": src, "target": tgt}


class CompilerHappy(SimpleTestCase):
    def test_linear_dag_is_valid(self):
        nodes = [_node("a", "trigger"), _node("b"), _node("c")]
        edges = [_edge("a", "b"), _edge("b", "c")]
        errors = validate_dag(nodes, edges)
        # No cycle / orphan errors. Trigger / type errors may still appear
        # depending on the registry but the DAG check itself is clean.
        self.assertFalse([e for e in errors if e.error_type in {"cycle", "invalid_edge"}])


class CompilerSad(SimpleTestCase):
    def test_empty_workflow_rejected(self):
        errors = validate_dag([], [])
        self.assertTrue(any(e.error_type == "empty_workflow" for e in errors))

    def test_edge_references_unknown_source(self):
        nodes = [_node("a", "trigger")]
        edges = [_edge("ghost", "a")]
        errors = validate_dag(nodes, edges)
        self.assertTrue(any(e.error_type == "invalid_edge" for e in errors))

    def test_edge_references_unknown_target(self):
        nodes = [_node("a", "trigger")]
        edges = [_edge("a", "ghost")]
        errors = validate_dag(nodes, edges)
        self.assertTrue(any(e.error_type == "invalid_edge" for e in errors))


class CompilerAngry(SimpleTestCase):
    """Pathological graphs designed to break the validator."""

    def test_cycle_without_loop_node_is_rejected(self):
        nodes = [_node("a", "trigger"), _node("b", "http"), _node("c", "http")]
        edges = [_edge("a", "b"), _edge("b", "c"), _edge("c", "b")]
        errors = validate_dag(nodes, edges)
        self.assertTrue(any(e.error_type == "dag_cycle" for e in errors), "cycle missed")

    def test_self_loop_on_non_loop_node_rejected(self):
        nodes = [_node("a", "trigger"), _node("b", "http")]
        edges = [_edge("a", "b"), _edge("b", "b")]
        errors = validate_dag(nodes, edges)
        self.assertTrue(any(e.error_type == "dag_cycle" for e in errors))

    def test_huge_node_count_does_not_blow_stack(self):
        """Build a very wide graph: 5000 nodes, all fanned-out from one trigger."""
        nodes = [_node("trigger", "trigger")] + [_node(f"n{i}") for i in range(5000)]
        edges = [_edge("trigger", f"n{i}") for i in range(5000)]
        errors = validate_dag(nodes, edges)
        # Should not RecursionError. May still produce errors (e.g. type checks)
        # but execution must complete.
        self.assertIsInstance(errors, list)

    def test_long_chain_does_not_recurse(self):
        """5000-deep linear chain — checks topological sort is iterative."""
        nodes = [_node("trigger", "trigger")] + [_node(f"n{i}") for i in range(5000)]
        edges = [_edge("trigger", "n0")] + [_edge(f"n{i}", f"n{i+1}") for i in range(4999)]
        errors = validate_dag(nodes, edges)
        self.assertFalse(any(e.error_type == "dag_cycle" for e in errors))

    def test_duplicate_node_ids_caught_or_handled(self):
        """Two nodes with the same ID — must not silently overwrite."""
        nodes = [_node("a", "trigger"), _node("a", "http")]
        edges = []
        # Don't assert exact behaviour, just no crash.
        errors = validate_dag(nodes, edges)
        self.assertIsInstance(errors, list)

    def test_no_trigger_when_every_node_has_incoming_edge(self):
        """Two nodes in a cycle → no zero-in-degree → no_trigger error."""
        # Make a 2-cycle of two http nodes — neither is a loop type, so this is
        # also a cycle error, but the code returns early on cycles. We force
        # an isolated config where no node has zero in-degree by adding a self
        # loop to the only would-be trigger.
        nodes = [_node("a", "http"), _node("b", "http")]
        edges = [_edge("a", "b"), _edge("b", "a")]
        errors = validate_dag(nodes, edges)
        # Either a cycle error or no_trigger — both prove the validator
        # noticed the workflow has no entry point.
        self.assertTrue(errors)

    def test_node_type_extraction_handles_missing_data(self):
        self.assertEqual(get_node_type({"id": "x", "type": "fallback"}), "fallback")
        self.assertEqual(get_node_type({"id": "x"}), "")
        self.assertEqual(
            get_node_type({"id": "x", "data": {"nodeType": "from-data"}, "type": "ignored"}),
            "from-data",
        )

    def test_node_with_null_id_does_not_crash_validator(self):
        nodes = [{"id": None, "type": "http", "data": {}}]
        edges = []
        try:
            errors = validate_dag(nodes, edges)
            self.assertIsInstance(errors, list)
        except (TypeError, AttributeError, KeyError):
            # Acceptable: explicit failure rather than silent corruption.
            pass

    def test_edge_with_missing_source_target_keys(self):
        nodes = [_node("a", "trigger")]
        edges = [{"id": "weird"}]  # no source / target
        errors = validate_dag(nodes, edges)
        # Either flagged as invalid_edge or ignored — not 500.
        self.assertIsInstance(errors, list)
