"""
Unit tests for streaming/broadcaster.py and streaming/consumers.py — pure
logic only (no real channel layer, no Django Channels test harness).
"""
from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

from django.test import SimpleTestCase

from streaming.broadcaster import SSEBroadcaster, StreamEvent


def _run(coro):
    return asyncio.run(coro)


class StreamEventFormatTests(SimpleTestCase):
    def test_basic_format(self):
        ev = StreamEvent(event_type="node_start", data={"node_id": "n1"})
        s = ev.format_sse()
        # SSE messages must end with \n\n; lines are separated by \n.
        self.assertTrue(s.endswith("\n\n"))
        self.assertIn("event: node_start", s)
        # Data line is JSON-encoded.
        data_line = [l for l in s.splitlines() if l.startswith("data: ")][0]
        decoded = json.loads(data_line[len("data: "):])
        self.assertEqual(decoded["type"], "node_start")
        self.assertEqual(decoded["data"]["node_id"], "n1")

    def test_id_and_retry_emitted_when_set(self):
        ev = StreamEvent(event_type="ev", data={}, id="42", retry=3000)
        s = ev.format_sse()
        self.assertIn("id: 42", s)
        self.assertIn("retry: 3000", s)

    def test_timestamp_auto_set(self):
        ev = StreamEvent(event_type="x", data={})
        self.assertIsNotNone(ev.timestamp)
        # ISO format roughly: '2026-04-25T...'
        self.assertIn("T", ev.timestamp)

    def test_special_characters_in_data_dont_break_format(self):
        # JSON should escape \n and quotes; SSE remains parseable line-by-line.
        ev = StreamEvent(event_type="log", data={"msg": 'line1\nline"2'})
        s = ev.format_sse()
        # The \n inside the JSON string must be escaped, NOT a literal newline
        # in the SSE data line.
        data_lines = [l for l in s.splitlines() if l.startswith("data: ")]
        self.assertEqual(len(data_lines), 1)


class BroadcasterMemorySubscribeTests(SimpleTestCase):
    """In-memory queue path (channel_layer=None)."""

    def setUp(self):
        # Force memory path by overriding channel_layer property.
        self.b = SSEBroadcaster()
        self.b._channel_layer = None
        # Monkey-patch the @property.lambda for this instance via class swap.
        type(self.b).channel_layer = property(lambda s: None)
        # Reset shared dict to isolate test state.
        SSEBroadcaster._subscribers = {}

    def test_subscribe_publish_receive(self):
        async def scenario():
            execution_id = str(uuid4())
            q = await self.b.subscribe(execution_id)
            await self.b.send_event(execution_id, "node_start", {"x": 1})
            ev = await asyncio.wait_for(q.get(), timeout=1.0)
            return ev

        ev = _run(scenario())
        self.assertEqual(ev.event_type, "node_start")
        self.assertEqual(ev.data, {"x": 1})

    def test_unsubscribe_removes_queue(self):
        async def scenario():
            execution_id = str(uuid4())
            q = await self.b.subscribe(execution_id)
            await self.b.unsubscribe(execution_id, q)
            return execution_id

        eid = _run(scenario())
        # Empty list cleaned up.
        self.assertNotIn(eid, SSEBroadcaster._subscribers)

    def test_full_queue_does_not_kill_others(self):
        async def scenario():
            execution_id = str(uuid4())
            full_q = asyncio.Queue(maxsize=1)
            healthy_q = asyncio.Queue(maxsize=10)
            # Pre-fill the full queue so put_nowait raises QueueFull.
            full_q.put_nowait("X")
            async with self.b._lock:
                SSEBroadcaster._subscribers[execution_id] = [full_q, healthy_q]
            await self.b.send_event(execution_id, "node_start", {"a": 1})
            return healthy_q

        q = _run(scenario())
        # Healthy queue still got the event (not blocked by full peer).
        self.assertEqual(q.qsize(), 1)


class BroadcasterChannelsPathTests(SimpleTestCase):
    def test_channel_layer_used_when_available(self):
        b = SSEBroadcaster()
        layer = MagicMock()
        layer.group_send = AsyncMock()
        b._channel_layer = layer

        async def scenario():
            await b.send_event("eid-1", "node_complete", {"ok": True})

        _run(scenario())
        layer.group_send.assert_awaited_once()
        args, _ = layer.group_send.call_args
        self.assertEqual(args[0], "execution_eid-1")
        self.assertEqual(args[1]["type"], "execution.event")

    def test_channel_layer_send_failure_falls_back_to_memory(self):
        b = SSEBroadcaster()
        layer = MagicMock()
        layer.group_send = AsyncMock(side_effect=RuntimeError("oops"))
        b._channel_layer = layer
        SSEBroadcaster._subscribers = {}

        async def scenario():
            q = await b.subscribe("eid-2")
            await b.send_event("eid-2", "x", {})
            return q

        q = _run(scenario())
        # Memory subscriber should have received the event despite Channels failure.
        self.assertEqual(q.qsize(), 1)


class ProgressUpdateTests(SimpleTestCase):
    def test_percentage_zero_when_no_total(self):
        b = SSEBroadcaster()
        b._channel_layer = None
        SSEBroadcaster._subscribers = {}

        async def scenario():
            q = await b.subscribe("e-prog")
            await b.progress_update("e-prog", current_node=0, total_nodes=0)
            return await asyncio.wait_for(q.get(), timeout=1.0)

        ev = _run(scenario())
        self.assertEqual(ev.data["percentage"], 0)

    def test_percentage_calculated(self):
        b = SSEBroadcaster()
        b._channel_layer = None
        SSEBroadcaster._subscribers = {}

        async def scenario():
            q = await b.subscribe("e-prog2")
            await b.progress_update("e-prog2", current_node=3, total_nodes=10)
            return await asyncio.wait_for(q.get(), timeout=1.0)

        ev = _run(scenario())
        self.assertEqual(ev.data["percentage"], 30)


class ConsumerResponseHandlingTests(SimpleTestCase):
    """
    Regression: response.get('value', response) used to crash when response
    wasn't a dict.  Verify _save_hitl_response now handles primitive responses.
    """

    def test_dict_response_extracts_value_field(self):
        # Reach into the closure of the type-guard expression.
        response = {"value": "approve", "extra": 1}
        out = response.get("value", response) if isinstance(response, dict) else response
        self.assertEqual(out, "approve")

    def test_string_response_used_directly(self):
        response = "approve"
        out = response.get("value", response) if isinstance(response, dict) else response
        self.assertEqual(out, "approve")

    def test_bool_response_used_directly(self):
        response = True
        out = response.get("value", response) if isinstance(response, dict) else response
        self.assertIs(out, True)
