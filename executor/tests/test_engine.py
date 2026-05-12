"""
Unit tests for executor/engine.py.

Focus on the pure helpers (_initial_state, _result_to_execution_state,
_heartbeat). The run_workflow/compile pipeline needs a real compiled graph
so it's exercised via integration tests, not here.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock
from uuid import uuid4

from django.test import SimpleTestCase

from executor.engine import _heartbeat, _initial_state, _result_to_execution_state
from orchestrator.interface import ExecutionState


class InitialStateTests(SimpleTestCase):
    def test_contains_all_required_keys(self):
        exec_id = uuid4()
        state = _initial_state(
            exec_id, workflow_id=1, user_id=2,
            input_data={"x": 1}, credentials={"c": "v"},
            parent_execution_id=None, nesting_depth=0,
            workflow_chain=None, timeout_budget_ms=None, skills=None,
        )
        # Regression: old engine omitted loop_stats, which was declared in
        # the WorkflowState TypedDict. The compiler tolerated this with a
        # defensive None-check, but the key must be present.
        self.assertIn("loop_stats", state)
        self.assertEqual(state["loop_stats"], {})
        self.assertEqual(state["execution_id"], str(exec_id))
        self.assertEqual(state["node_outputs"]["_input_global"], {"x": 1})
        self.assertEqual(state["status"], "running")

    def test_parent_execution_id_stringified(self):
        pid = uuid4()
        state = _initial_state(
            uuid4(), workflow_id=1, user_id=2,
            input_data=None, credentials=None,
            parent_execution_id=pid, nesting_depth=2,
            workflow_chain=[10, 20], timeout_budget_ms=5000, skills=[],
        )
        self.assertEqual(state["parent_execution_id"], str(pid))
        self.assertEqual(state["workflow_chain"], [10, 20])

    def test_none_collections_become_empty(self):
        state = _initial_state(
            uuid4(), workflow_id=1, user_id=2,
            input_data=None, credentials=None,
            parent_execution_id=None, nesting_depth=0,
            workflow_chain=None, timeout_budget_ms=None, skills=None,
        )
        self.assertEqual(state["node_outputs"]["_input_global"], {})
        self.assertEqual(state["credentials"], {})
        self.assertEqual(state["skills"], [])


class ResultToExecutionStateTests(SimpleTestCase):
    def test_maps_known_statuses(self):
        self.assertEqual(_result_to_execution_state("failed"), ExecutionState.FAILED)
        self.assertEqual(_result_to_execution_state("cancelled"), ExecutionState.CANCELLED)
        self.assertEqual(_result_to_execution_state("paused"), ExecutionState.PAUSED)
        self.assertEqual(_result_to_execution_state("completed"), ExecutionState.COMPLETED)

    def test_unknown_defaults_to_completed(self):
        self.assertEqual(_result_to_execution_state("weird"), ExecutionState.COMPLETED)


class HeartbeatTests(SimpleTestCase):
    def test_cancels_cleanly_on_exit(self):
        async def scenario():
            exec_logger = AsyncMock()
            exec_id = uuid4()
            hb = _heartbeat(exec_logger, exec_id)
            async with hb:
                # Block briefly so the pulse task is definitely scheduled.
                await asyncio.sleep(0)
            # After exit, the task must be cancelled and awaited.
            self.assertTrue(hb._task.done())

        asyncio.run(scenario())
