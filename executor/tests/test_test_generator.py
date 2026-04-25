"""Unit tests for executor/test_generator.py."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from django.test import SimpleTestCase

from executor.test_generator import (
    ValidationResult, generate_test_input, validate_test_result,
)


def _wf(nodes):
    """Build a minimal workflow-like object with just the `nodes` attr."""
    return SimpleNamespace(nodes=nodes)


class GenerateTestInputTests(SimpleTestCase):
    def test_manual_trigger_detected_via_nodeType(self):
        # Regression: old code used node['type'], missing nodeType-shaped
        # triggers produced by the frontend.
        wf = _wf([{"id": "t", "data": {"nodeType": "manual_trigger"}}])
        data = generate_test_input(wf)
        self.assertIn("text", data)
        self.assertIn("number", data)
        self.assertIn("boolean", data)

    def test_webhook_trigger(self):
        wf = _wf([{"id": "t", "data": {"nodeType": "webhook_trigger"}}])
        data = generate_test_input(wf)
        self.assertEqual(data["headers"]["Content-Type"], "application/json")

    def test_non_trigger_nodes_ignored(self):
        wf = _wf([{"id": "c", "data": {"nodeType": "code"}}])
        self.assertEqual(generate_test_input(wf), {})

    def test_no_nodes(self):
        self.assertEqual(generate_test_input(_wf(None)), {})


class ValidateTestResultTests(SimpleTestCase):
    def test_passes_without_schema(self):
        self.assertTrue(validate_test_result({"a": 1}).passed)

    def test_fails_on_missing_key(self):
        r = validate_test_result({"a": 1}, expected_schema={"b": int})
        self.assertFalse(r.passed)
        self.assertIn("Missing expected output key: b", r.error)

    def test_passes_on_schema_match(self):
        r = validate_test_result({"a": 1, "b": 2}, expected_schema={"a": int})
        self.assertTrue(r.passed)
