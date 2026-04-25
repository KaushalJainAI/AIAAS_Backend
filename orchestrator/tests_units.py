"""
Unit tests for orchestrator — pure-logic helpers that don't require DB or HTTP.

The larger CRUD / execution-control API tests live in tests.py /
tests_partial.py / tests_security.py and require the full Django test
harness; this file focuses on in-process behaviour we can exercise fast.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock
from uuid import uuid4

from django.test import SimpleTestCase

from orchestrator.approval_gates import (
    ApprovalAction, ApprovalGate, ApprovalGateManager, ApprovalResult,
)
from orchestrator.views import is_functionally_identical


class IsFunctionallyIdenticalTests(SimpleTestCase):
    def test_layout_only_change_is_identical(self):
        # Changing `position` should NOT trigger a functional diff.
        a_nodes = [{"id": "n1", "type": "code", "position": {"x": 0, "y": 0},
                    "data": {"nodeType": "code", "config": {"x": 1}}}]
        b_nodes = [{"id": "n1", "type": "code", "position": {"x": 500, "y": 500},
                    "data": {"nodeType": "code", "config": {"x": 1}}}]
        edges = []
        self.assertTrue(is_functionally_identical(a_nodes, edges, b_nodes, edges))

    def test_config_change_is_different(self):
        a = [{"id": "n1", "type": "code", "data": {"nodeType": "code", "config": {"x": 1}}}]
        b = [{"id": "n1", "type": "code", "data": {"nodeType": "code", "config": {"x": 2}}}]
        self.assertFalse(is_functionally_identical(a, [], b, []))

    def test_edge_order_doesnt_matter(self):
        edges_a = [
            {"id": "e1", "source": "n1", "target": "n2"},
            {"id": "e2", "source": "n2", "target": "n3"},
        ]
        edges_b = list(reversed(edges_a))
        nodes = [
            {"id": "n1", "type": "t", "data": {"nodeType": "t", "config": {}}},
            {"id": "n2", "type": "t", "data": {"nodeType": "t", "config": {}}},
            {"id": "n3", "type": "t", "data": {"nodeType": "t", "config": {}}},
        ]
        self.assertTrue(is_functionally_identical(nodes, edges_a, nodes, edges_b))


class ApprovalGateLogicTests(SimpleTestCase):
    """
    Tests the approval gate's pure state machine without hitting the DB or
    websocket consumer. _save_to_database / _send_notification are patched
    to no-ops by monkey-patching async methods.
    """

    def _gate(self, **kwargs) -> ApprovalGate:
        g = ApprovalGate(
            execution_id=uuid4(), node_id="n1", user_id=1,
            message="approve?", **kwargs,
        )
        g._save_to_database = AsyncMock()
        g._update_database = AsyncMock()
        g._send_notification = AsyncMock()
        return g

    def test_approve_response_returns_approved(self):
        async def scenario():
            gate = self._gate(timeout_seconds=5)
            # Pre-populate the queue so wait_for_approval returns immediately.
            await gate._response_queue.put({"action": "approve"})
            result = await gate.wait_for_approval(timeout=1)
            return result

        result = asyncio.run(scenario())
        self.assertIsInstance(result, ApprovalResult)
        self.assertTrue(result.approved)
        self.assertEqual(result.action, ApprovalAction.APPROVE)
        self.assertFalse(result.timed_out)

    def test_reject_response(self):
        async def scenario():
            gate = self._gate()
            await gate._response_queue.put({"action": "reject"})
            return await gate.wait_for_approval(timeout=1)

        result = asyncio.run(scenario())
        self.assertFalse(result.approved)
        self.assertTrue(result.rejected)

    def test_timeout_returns_auto_action(self):
        async def scenario():
            gate = self._gate(auto_action=ApprovalAction.APPROVE)
            return await gate.wait_for_approval(timeout=0.01)

        result = asyncio.run(scenario())
        self.assertTrue(result.timed_out)
        # auto_action was APPROVE → approved flag True.
        self.assertTrue(result.approved)


class ApprovalGateManagerTests(SimpleTestCase):
    def test_register_and_submit_roundtrip(self):
        async def scenario():
            mgr = ApprovalGateManager()
            gate = ApprovalGate(
                execution_id=uuid4(), node_id="n1", user_id=5, message="go?",
            )
            mgr.register(gate)
            ok = await mgr.submit_response(gate.request_id, {"action": "approve"})
            queued = await gate._response_queue.get()
            return ok, queued

        ok, queued = asyncio.run(scenario())
        self.assertTrue(ok)
        self.assertEqual(queued["action"], "approve")

    def test_submit_unknown_returns_false(self):
        async def scenario():
            mgr = ApprovalGateManager()
            return await mgr.submit_response(uuid4(), {"action": "approve"})

        self.assertFalse(asyncio.run(scenario()))

    def test_get_pending_for_user_filters(self):
        mgr = ApprovalGateManager()
        g1 = ApprovalGate(execution_id=uuid4(), node_id="n", user_id=1, message="")
        g2 = ApprovalGate(execution_id=uuid4(), node_id="n", user_id=2, message="")
        mgr.register(g1)
        mgr.register(g2)
        pending = mgr.get_pending_for_user(1)
        self.assertEqual([g.user_id for g in pending], [1])
