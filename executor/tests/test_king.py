"""
Unit tests for KingOrchestrator — focused on logic that doesn't require a
running event loop, database, or LLM provider.

Full lifecycle tests (start/pause/resume/stop against a real graph) belong
in an integration suite.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from django.test import SimpleTestCase

from executor.exceptions import LLMProviderError
from executor.king import ExecutionHandle, KingOrchestrator
from orchestrator.interface import ExecutionState, SupervisionLevel


class ExecutionHandleGoalTests(SimpleTestCase):
    """Goal-condition evaluation — pure logic, no I/O."""

    def _handle(self, **overrides) -> ExecutionHandle:
        defaults = dict(execution_id=uuid4(), workflow_id=1, user_id=1)
        defaults.update(overrides)
        return ExecutionHandle(**defaults)

    def test_min_rows_fails_when_under(self):
        h = self._handle(goal_conditions={"min_rows": 5})
        ok, reason = h.check_goal_condition([1, 2])
        self.assertFalse(ok)
        self.assertIn("minimum is 5", reason)

    def test_min_rows_passes_when_above(self):
        h = self._handle(goal_conditions={"min_rows": 2})
        ok, _ = h.check_goal_condition([1, 2, 3])
        self.assertTrue(ok)

    def test_max_errors_triggers_stop(self):
        h = self._handle(goal_conditions={"max_errors": 2})
        h.record_error("n1", "boom")
        h.record_error("n2", "boom")
        h.record_error("n3", "boom")
        ok, reason = h.check_goal_condition({})
        self.assertFalse(ok)
        self.assertIn("Too many errors", reason)

    def test_output_should_stop_flag(self):
        h = self._handle()
        ok, reason = h.check_goal_condition({"should_stop": True, "stop_reason": "user requested"})
        self.assertFalse(ok)
        self.assertEqual(reason, "user requested")

    def test_record_node_output_indexed(self):
        h = self._handle()
        h.record_node_output("n1", {"val": 42})
        self.assertEqual(h.node_outputs["n1"], {"val": 42})


class ModifyPromptPresenceTests(SimpleTestCase):
    """MODIFY_PROMPT used to be missing — guarding against regression."""

    def test_modify_prompt_defined_and_formatable(self):
        tmpl = KingOrchestrator.MODIFY_PROMPT
        self.assertIn("{workflow_json}", tmpl)
        self.assertIn("{modification}", tmpl)
        self.assertIn("{node_types}", tmpl)
        # Formatting with minimal values should not raise KeyError.
        out = tmpl.format(
            node_types="- code: ...", workflow_json="{}", modification="do X",
        )
        self.assertIn("do X", out)


class LLMProviderErrorTests(SimpleTestCase):
    """Ensure _call_llm raises the typed error."""

    def test_llm_provider_error_classifies_connection(self):
        # Build an orchestrator but short-circuit everything except the
        # error-branch we want to cover.
        async def run():
            king = KingOrchestrator(user_id=1)
            king.settings_loaded = True

            handler = AsyncMock()
            handler.execute.return_value = type(
                "R", (), {"success": False, "error": "connection refused by host"},
            )()
            fake_registry = type("Reg", (), {
                "has_handler": lambda self, t: True,
                "get_handler": lambda self, t: handler,
            })()

            with patch.object(king, "_get_registry", return_value=fake_registry):
                with self.assertRaises(LLMProviderError) as cm:
                    await king._call_llm("hi", user_id=1)
                self.assertTrue(cm.exception.is_connection_error)

        asyncio.run(run())

    def test_llm_provider_error_content_class(self):
        async def run():
            king = KingOrchestrator(user_id=1)
            king.settings_loaded = True

            handler = AsyncMock()
            handler.execute.return_value = type(
                "R", (), {"success": False, "error": "malformed response"},
            )()
            fake_registry = type("Reg", (), {
                "has_handler": lambda self, t: True,
                "get_handler": lambda self, t: handler,
            })()

            with patch.object(king, "_get_registry", return_value=fake_registry):
                with self.assertRaises(LLMProviderError) as cm:
                    await king._call_llm("hi", user_id=1)
                self.assertFalse(cm.exception.is_connection_error)

        asyncio.run(run())


class ParseJsonResponseTests(SimpleTestCase):
    def setUp(self):
        self.king = KingOrchestrator(user_id=1)

    def test_strips_markdown_fence(self):
        raw = '```json\n{"a": 1}\n```'
        self.assertEqual(self.king._parse_json_response(raw), {"a": 1})

    def test_extracts_json_from_mixed_text(self):
        raw = 'Here is the output: {"b": 2} end'
        self.assertEqual(self.king._parse_json_response(raw), {"b": 2})

    def test_invalid_raises(self):
        with self.assertRaises(ValueError):
            self.king._parse_json_response("no json here")


class ValidateWorkflowTests(SimpleTestCase):
    def setUp(self):
        self.king = KingOrchestrator(user_id=1)

    def test_accepts_well_formed(self):
        wf = {
            "nodes": [{"id": "n1", "type": "code", "position": {"x": 0, "y": 0}}],
            "edges": [],
        }
        self.assertTrue(self.king._validate_workflow(wf))

    def test_rejects_missing_edges_key(self):
        self.assertFalse(self.king._validate_workflow({"nodes": []}))

    def test_rejects_node_missing_required_field(self):
        wf = {
            "nodes": [{"id": "n1", "type": "code"}],  # missing position
            "edges": [],
        }
        self.assertFalse(self.king._validate_workflow(wf))
