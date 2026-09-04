"""
What happens to the *other* tool calls when one of them pauses for approval.

`interrupt()` unwinds the node and discards its writes, then re-runs it from
the top on resume. Graph state rolls back cleanly; the outside world does not.
So a batch of [safe, sensitive] executed inline would run the safe call, pause,
and run the safe call a second time when the user approved — two emails, two
`AgentStep` rows, two rounds of UI side effects, from one request.

The fix is that `tools_node` settles every permission before it dispatches
anything, which is what makes its re-run idempotent. These tests are the reason
that shape cannot be quietly refactored back into one loop.

The rejection cases cover the other half: before `reject_tool_call` existed,
declining a call recorded nothing the graph could act on, so the run stayed
paused for ever.
"""
from __future__ import annotations

import uuid
from unittest.mock import patch

from django.test import TestCase

from asgiref.sync import async_to_sync

from chat.turn.agent import (
    TurnContext,
    approve_tool_call,
    reject_tool_call,
    run_turn,
)
from llm.access import Completion, ToolCall


def _completion(*calls: tuple[str, dict], content: str = "") -> Completion:
    return Completion(
        content=content,
        tool_calls=tuple(
            ToolCall(id=f"call-{name}", name=name, arguments=args)
            for name, args in calls
        ),
    )


class _Harness:
    """Drives a real turn with a scripted model and a counting dispatcher."""

    def __init__(self, *scripted: Completion):
        self.scripted = list(scripted)
        self.dispatched: list[str] = []
        self.thread_id = f"test-{uuid.uuid4()}"

    async def model(self, turn, *, prompt, history, tools, **_):
        return self.scripted.pop(0) if self.scripted else Completion(content="done")

    async def dispatch(self, name, args, context):
        self.dispatched.append(name)
        return f"{name} ran"

    async def tools(self):
        return []

    def context(self, **overrides) -> TurnContext:
        base = dict(
            provider="openrouter",
            model="test/model",
            system_message="",
            user_id=1,
            session_id=self.thread_id,
            intent="chat",
            user_text="go",
            memory_enabled=False,
            tool_source=self.tools,
            tool_dispatch=self.dispatch,
            sensitive_tools=frozenset({"send_email"}),
        )
        base.update(overrides)
        return TurnContext(**base)

    def run(self):
        with patch("chat.turn.agent._run_model", self.model):
            return async_to_sync(run_turn)(
                self.context(), prompt="go", thread_id=self.thread_id
            )


class ApprovalReplayTests(TestCase):
    def test_a_safe_call_is_not_dispatched_before_the_batch_pauses(self):
        """The regression: safe work must not run until every gate is settled.

        If `web_search` runs here, it will run *again* after approval, because
        the interrupt discards the node's writes but not its side effects.
        """
        harness = _Harness(_completion(
            ("web_search", {"query": "x"}), ("send_email", {"to": "a@b.c"}),
        ))
        result = harness.run()

        self.assertTrue(result.awaiting_approval)
        self.assertEqual(harness.dispatched, [])

    def test_approving_dispatches_each_call_exactly_once(self):
        harness = _Harness(_completion(
            ("web_search", {"query": "x"}), ("send_email", {"to": "a@b.c"}),
        ))
        harness.run()

        async_to_sync(approve_tool_call)(harness.thread_id, "call-send_email")
        harness.scripted = [Completion(content="all done")]
        result = harness.run()

        self.assertFalse(result.awaiting_approval)
        # The heart of it: one dispatch each, not two for the safe call.
        self.assertEqual(harness.dispatched, ["web_search", "send_email"])
        self.assertEqual(result.answer, "all done")

    def test_a_batch_with_no_sensitive_call_still_dispatches_in_order(self):
        harness = _Harness(_completion(
            ("web_search", {"query": "x"}), ("read_url", {"url": "u"}),
        ))
        harness.run()

        self.assertEqual(harness.dispatched, ["web_search", "read_url"])

    def test_two_sensitive_calls_pause_once_each(self):
        harness = _Harness(_completion(
            ("send_email", {"to": "a"}), ("send_email2", {"to": "b"}),
        ), )
        harness_ctx = harness.context(
            sensitive_tools=frozenset({"send_email", "send_email2"})
        )
        with patch("chat.turn.agent._run_model", harness.model):
            first = async_to_sync(run_turn)(
                harness_ctx, prompt="go", thread_id=harness.thread_id
            )
        self.assertTrue(first.awaiting_approval)
        self.assertEqual(harness.dispatched, [])

        async_to_sync(approve_tool_call)(harness.thread_id, "call-send_email")
        with patch("chat.turn.agent._run_model", harness.model):
            second = async_to_sync(run_turn)(
                harness_ctx, prompt="go", thread_id=harness.thread_id
            )
        # Still paused on the second call, and *neither* has run yet.
        self.assertTrue(second.awaiting_approval)
        self.assertEqual(harness.dispatched, [])


class RejectionTests(TestCase):
    def test_rejecting_lets_the_run_continue_without_the_call(self):
        harness = _Harness(_completion(("send_email", {"to": "a@b.c"})))
        first = harness.run()
        self.assertTrue(first.awaiting_approval)

        recorded = async_to_sync(reject_tool_call)(
            harness.thread_id, "call-send_email", reason="Wrong recipient.",
        )
        self.assertTrue(recorded)

        harness.scripted = [Completion(content="Understood, skipping the email.")]
        result = harness.run()

        # The run finished rather than staying paused for ever, and the tool
        # never ran.
        self.assertFalse(result.awaiting_approval)
        self.assertEqual(harness.dispatched, [])
        self.assertEqual(result.answer, "Understood, skipping the email.")

    def test_the_model_is_told_what_was_refused_and_why(self):
        harness = _Harness(_completion(("send_email", {"to": "a@b.c"})))
        harness.run()
        async_to_sync(reject_tool_call)(
            harness.thread_id, "call-send_email", reason="Wrong recipient.",
        )

        seen: list[dict] = []

        async def capture(turn, *, prompt, history, tools, **_):
            seen.append({"prompt": prompt, "history": list(history)})
            return Completion(content="ok")

        with patch("chat.turn.agent._run_model", capture):
            async_to_sync(run_turn)(
                harness.context(), prompt="go", thread_id=harness.thread_id
            )

        rendered = str(seen[-1])
        self.assertIn("declined", rendered)
        self.assertIn("Wrong recipient.", rendered)

    def test_a_rejected_call_still_answers_its_tool_call_id(self):
        """A dangling `tool_call_id` is a malformed transcript.

        The assistant turn asked for the call by id; every provider requires a
        `tool` message answering it, refused or not.
        """
        harness = _Harness(_completion(
            ("web_search", {"query": "x"}), ("send_email", {"to": "a"}),
        ))
        harness.run()
        async_to_sync(reject_tool_call)(harness.thread_id, "call-send_email")

        seen: list[list] = []

        async def capture(turn, *, prompt, history, tools, **_):
            seen.append(list(history))
            return Completion(content="ok")

        harness.scripted = []
        with patch("chat.turn.agent._run_model", capture):
            async_to_sync(run_turn)(
                harness.context(), prompt="go", thread_id=harness.thread_id
            )

        answered = {
            m.get("tool_call_id") for m in seen[-1] if m.get("role") == "tool"
        }
        self.assertEqual(answered, {"call-web_search", "call-send_email"})
        # The approved half of the batch really ran; the refused half did not.
        self.assertEqual(harness.dispatched, ["web_search"])

    def test_rejecting_an_unknown_thread_reports_rather_than_pretends(self):
        self.assertFalse(
            async_to_sync(reject_tool_call)("no-such-thread", "call-x")
        )
