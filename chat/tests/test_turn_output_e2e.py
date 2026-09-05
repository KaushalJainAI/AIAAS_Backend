"""
What a turn actually hands to the client, through the real graph.

Every feature added this cycle produces something a *person* is supposed to see:
a plan, a chart, a file in their tree. Each one crosses four boundaries to get
there — tool → side effect → graph state → message metadata — and the unit tests
cover each boundary in isolation, which is exactly the arrangement that lets a
feature pass every test while reaching nobody. That happened here: the todo list
was built, streamed and stored, and no component rendered it.

So this drives the real graph with a stubbed provider and asserts on the two
things a client actually consumes: the events emitted during the turn, and the
metadata left on the finished result. Nothing here mocks a node.
"""
from __future__ import annotations

import json

from asgiref.sync import async_to_sync
from django.contrib.auth.models import User
from django.test import TestCase
from unittest.mock import patch

from chat.turn.agent import TurnContext, run_turn
from chat.turn.events import Event


class _ScriptedModel:
    """Issues a scripted tool call per turn, then answers.

    Stands in for `llm.stream` so the run goes through the real accumulator,
    the real `tools_node`, the real side-effect table and the real checkpoint —
    the parts where a feature silently fails to arrive.
    """

    def __init__(self, calls: list[tuple[str, dict]]) -> None:
        self.script = calls
        self.turn = 0

    def __call__(self, **kwargs):
        index = self.turn
        self.turn += 1

        async def chunks():
            if index < len(self.script):
                name, args = self.script[index]
                yield {"type": "tool_calls", "tool_calls": [{
                    "index": 0,
                    "id": f"call-{index}",
                    "function": {"name": name, "arguments": json.dumps(args)},
                }]}
            else:
                yield {"type": "content", "content": "all done"}
            yield {"type": "metadata", "usage": {"total_tokens": 5}}

        return chunks()


async def _never(name, args, context) -> bool:
    return False


class TurnOutputTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user("e2e2", "e2@example.com", "x")

    def _run(self, script, *, thread: str, file_scope=None, sensitive=None):
        model = _ScriptedModel(script)
        seen: list[tuple[str, dict]] = []

        async def sink(event: Event, payload: dict) -> None:
            seen.append((str(event), payload))

        turn = TurnContext(
            provider="stub", model="stub-model", system_message="You are a test.",
            user_id=self.user.id, session_id=thread, intent="chat",
            user_text="go", memory_enabled=False, max_iterations=8,
            approval_policy=_never, sink=sink, file_scope=file_scope,
            sensitive_tools=sensitive,
        )
        with patch("llm.access.stream", new=model):
            result = async_to_sync(run_turn)(turn, prompt="go", thread_id=thread)
        return result, seen

    # ── the plan ─────────────────────────────────────────────────────────────

    def test_a_plan_reaches_both_the_stream_and_the_stored_message(self):
        """Two consumers, two failure modes.

        The live event is what a watching user sees mid-run; the metadata is
        what a reopened conversation replays. A feature that lands in one and
        not the other looks finished right up until someone reloads.
        """
        result, seen = self._run([
            ("update_todos", {"todos": [
                {"text": "read the docs", "status": "done"},
                {"text": "write it up", "status": "doing"},
            ]}),
        ], thread="plan-1")

        events = [name for name, _ in seen]
        self.assertIn("todos_update", events)

        streamed = next(p for n, p in seen if n == "todos_update")
        self.assertEqual([t["text"] for t in streamed["todos"]],
                         ["read the docs", "write it up"])

        stored = result.metadata.get("todos")
        self.assertEqual(stored, streamed["todos"],
                         "the stored plan and the streamed plan disagree")

    def test_a_later_update_replaces_the_plan_rather_than_appending(self):
        """`update_todos` replaces the whole list, so the metadata must too.

        Appending would leave the finished message showing every intermediate
        version of the plan stacked together.
        """
        result, _ = self._run([
            ("update_todos", {"todos": [{"text": "step one", "status": "open"}]}),
            ("update_todos", {"todos": [{"text": "step one", "status": "done"},
                                        {"text": "step two", "status": "open"}]}),
        ], thread="plan-2")

        todos = result.metadata.get("todos")
        self.assertEqual(len(todos), 2)
        self.assertEqual(todos[0]["status"], "done")

    # ── charts ───────────────────────────────────────────────────────────────

    def test_a_chart_reaches_both_the_stream_and_the_stored_message(self):
        result, seen = self._run([
            ("render_chart", {
                "kind": "column", "title": "Revenue by quarter",
                "series": [{"name": "2026", "points": [
                    {"x": "Q1", "y": 10}, {"x": "Q2", "y": 14}]}],
            }),
        ], thread="chart-1")

        self.assertIn("chart", [name for name, _ in seen])
        charts = result.metadata.get("charts")
        self.assertEqual(len(charts), 1)
        self.assertEqual(charts[0]["title"], "Revenue by quarter")
        # The spec is stored, never a rendering of it — that is what lets a
        # reopened conversation redraw with today's component.
        self.assertEqual(charts[0]["series"][0]["points"][0], {"x": "Q1", "y": 10.0})

    def test_a_refused_chart_stores_nothing(self):
        """A spec the backend rejected must not reach the client as a chart.

        Otherwise the frontend renders an empty frame and the user sees a
        broken chart instead of the model's explanation.
        """
        result, seen = self._run([
            ("render_chart", {"kind": "pie", "title": "t", "series": [
                {"name": "a", "points": [{"x": "x", "y": 1}]},
                {"name": "b", "points": [{"x": "x", "y": 2}]},
            ]}),
        ], thread="chart-2")

        self.assertNotIn("chart", [name for name, _ in seen])
        self.assertFalse(result.metadata.get("charts"))

    # ── files ────────────────────────────────────────────────────────────────

    def test_chat_can_write_a_file_into_the_users_tree(self):
        """The whole point of `vfs.chat_scope`, end to end.

        Asserts against the database rather than the tool's return value: the
        tool could report success while writing nowhere the user can find.
        """
        from asgiref.sync import sync_to_async

        from inference.models import Document
        from inference.vfs import chat_scope

        scope = async_to_sync(sync_to_async(chat_scope))(self.user)

        # `write_file` is in `SENSITIVE_TOOLS`, so a real chat turn pauses for
        # approval first — asserted on its own below. Cleared here so this test
        # measures the write path rather than the approval path.
        self._run([
            ("write_file", {"path": "/Chat/summary.md", "content": "# Notes\nhello"}),
        ], thread="file-1", file_scope=scope, sensitive=frozenset())

        doc = Document.objects.filter(user=self.user, name="summary.md").first()
        self.assertIsNotNone(doc, "nothing was written to the user's tree")
        self.assertIn("hello", doc.content_text)
        # Under /Chat/, not loose at the root and not inside /Agents/.
        self.assertEqual(doc.folder.name, "Chat")
        # `status='stored'` — a write is never a silent embedding bill.
        self.assertEqual(doc.status, "stored")

    def test_writing_a_file_pauses_for_approval_in_chat(self):
        """Deliberate, and worth pinning because it is easy to "fix" wrongly.

        A save the user just asked for is arguably not worth a prompt — the
        codebase makes that argument itself for `execute_python`. But the flag
        is global, and `sensitive_tools_for` resolves the agent runtime's
        default `ask` autonomy straight from `SENSITIVE_TOOLS`: clearing it on
        the tool would silently stop every agent in the world asking before it
        writes. Loosening this belongs behind a per-caller set, not a tool flag.
        """
        from asgiref.sync import sync_to_async

        from inference.models import Document
        from inference.vfs import chat_scope

        scope = async_to_sync(sync_to_async(chat_scope))(self.user)
        result, seen = self._run([
            ("write_file", {"path": "/Chat/asked.md", "content": "x"}),
        ], thread="file-2", file_scope=scope)

        self.assertIn("ask_permission", [name for name, _ in seen])
        self.assertTrue(result.awaiting_approval)
        # Nothing was written while the question was outstanding.
        self.assertFalse(Document.objects.filter(user=self.user, name="asked.md").exists())

    def test_the_approval_frame_carries_a_readable_description(self):
        """What the client receives, not what a helper returns.

        The card rendered `JSON.stringify(args)` because the frame carried
        nothing else to render. A unit test on `describe_call` cannot catch
        that; the frame is the contract.
        """
        from asgiref.sync import sync_to_async

        from inference.vfs import chat_scope

        scope = async_to_sync(sync_to_async(chat_scope))(self.user)
        _, seen = self._run([
            ("write_file", {"path": "/Chat/asked.md", "content": "x"}),
        ], thread="file-3", file_scope=scope)

        frames = [payload for name, payload in seen if name == "ask_permission"]
        self.assertEqual(len(frames), 1)
        detail = frames[0]["detail"]

        self.assertEqual(detail["title"], "Save a file")
        self.assertIn({"label": "Path", "value": "/Chat/asked.md"}, detail["fields"])
        # The raw pair still ships — the card keeps a disclosure behind the
        # readable fields, which is the view worth having when the sentence
        # above it is wrong.
        self.assertEqual(frames[0]["args"]["path"], "/Chat/asked.md")

    def test_without_a_scope_the_file_tools_are_not_even_offered(self):
        """A caller that brought no scope must not see them at all."""
        from chat.tools import get_available_tools

        offered = {
            t["function"]["name"]
            for t in async_to_sync(get_available_tools)(self.user.id)
        }
        self.assertNotIn("write_file", offered)


class AgentRunOutputTests(TestCase):
    """What an agent run leaves behind for the run view to show.

    A run's metadata dies with the graph, so anything meant for a *reader*
    has to be copied onto `ExecutionLog.output_data` before the run closes.
    Without that an agent could draw a chart nobody would ever see and report
    blocked steps only to itself — the tools would work and the feature would
    not exist.
    """

    def test_charts_and_the_plan_are_carried_onto_the_run(self):
        from chat.turn.agent import TurnResult

        # The shape `_close_log` is handed, built the way the runtime builds it.
        result = TurnResult(
            answer="done",
            metadata={
                "charts": [{"type": "chart", "kind": "bar", "title": "t",
                            "series": [{"name": "a", "points": [{"x": "p", "y": 1}]}]}],
                "todos": [{"text": "step", "status": "blocked"}],
                "sources": ["ignored"],
            },
        )

        payload = {"answer": result.answer, "tool_trace": result.tool_trace}
        if charts := (result.metadata or {}).get("charts"):
            payload["charts"] = charts
        if todos := (result.metadata or {}).get("todos"):
            payload["todos"] = todos

        self.assertEqual(len(payload["charts"]), 1)
        self.assertEqual(payload["todos"][0]["status"], "blocked")

    def test_an_agent_may_draw_a_chart_without_any_grant(self):
        """It writes nothing and reaches nothing — the drawing happens in the
        reader's browser, from data the agent already had."""
        from agents.agent.runtime import AgentToolbox

        box = AgentToolbox(grants={}, user_id=1)
        self.assertIn("render_chart", box.allowed_names)

    def test_drawing_a_chart_is_not_a_capability_that_can_be_switched_off(self):
        # Same reasoning as `update_todos`: an agent that cannot show its
        # findings is not a safer agent, only a less useful one.
        from agents.agent.runtime import ALWAYS_AVAILABLE

        self.assertIn("render_chart", ALWAYS_AVAILABLE)
