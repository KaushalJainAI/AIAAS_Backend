"""
Tests for the handler calling convention — the result types and schema
helpers that moved here from the deleted `nodes` app.

Pure logic only: no DB, no real LLM/HTTP.
"""
from __future__ import annotations

from django.test import SimpleTestCase

from llm.handlers.base import (
    NodeExecutionResult,
    build_json_schema_from_fields,
    format_schema_for_prompt,
)


# ─────────────────────────────────────────────────────────────────────────
# NodeExecutionResult
# ─────────────────────────────────────────────────────────────────────────

class NodeExecutionResultTests(SimpleTestCase):
    def test_data_defaults_to_empty_dict(self):
        r = NodeExecutionResult(success=True)
        self.assertEqual(r.data, {})

    def test_data_roundtrips(self):
        r = NodeExecutionResult(success=True, data={"a": 1})
        self.assertEqual(r.data, {"a": 1})

    def test_none_data_coerced_to_empty_dict(self):
        # Handlers build results from parsed JSON that can legitimately be null.
        self.assertEqual(NodeExecutionResult(data=None).data, {})

    def test_failure_carries_error_and_status(self):
        r = NodeExecutionResult(success=False, error="boom", status_code=402)
        self.assertFalse(r.success)
        self.assertEqual(r.error, "boom")
        self.assertEqual(r.status_code, 402)


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