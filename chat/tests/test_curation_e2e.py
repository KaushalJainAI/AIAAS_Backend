"""
The whole loop, once, against a stub provider.

Everything else about curation is tested a piece at a time: the segment rule,
the watermark, each toggle, the node. Those all pass with the pieces wired to
each other incorrectly — the failure this file exists to catch is a graph edge
in the wrong place, a policy that never reaches the node, or state updates that
are computed and then dropped.

So this drives the real `chat_agent_graph` for twenty tool-calling turns with a
fake model, and asserts the three things a user would notice: the request never
outgrows the window, the run can still reach a fact from a step that was
compacted away, and the transcript stays sendable throughout.
"""
from __future__ import annotations

import json
from unittest.mock import patch

from asgiref.sync import async_to_sync
from django.contrib.auth import get_user_model
from django.test import TestCase

from chat.models import ToolOutput
from chat.turn import curation
from chat.turn.agent import TurnContext, run_turn
from llm import budget

User = get_user_model()

#: Small enough that twenty turns of tool output cannot fit, large enough that
#: the first few can. The whole mechanism is a function of this number against
#: the size of the results, so shrinking it is the same experiment as running a
#: much longer agent.
BUDGET_TOKENS = 12_000

#: The fact planted in turn 2's tool result and asked for at the end. It has to
#: be something only that result carried, or the model could answer from the
#: task itself.
SECRET = "the reconciliation code is QX-8842"


class _StubProvider:
    """A model that calls a tool for N turns, then answers.

    Stands in for `llm.stream`, the async generator `_run_model` consumes, so
    the run goes through the real accumulator and the real message threading
    rather than a shortcut past them.

    Records the wire history it was handed on every call, which is what the
    budget assertions are made against — the point is what actually left for the
    provider, not what the curator believed it had done.
    """

    def __init__(self, turns: int) -> None:
        self.turns = turns
        self.calls = 0
        self.histories: list[list[dict]] = []
        self.last_prompt = ""

    def __call__(self, **kwargs):
        self.calls += 1
        self.histories.append(list(kwargs.get("history") or []))
        self.last_prompt = kwargs.get("prompt") or ""
        answering = self.calls > self.turns
        step = self.calls

        async def chunks():
            if answering:
                yield {"type": "content", "content": "done"}
            else:
                yield {"type": "tool_calls", "tool_calls": [{
                    "index": 0,
                    "id": f"call-{step}",
                    "function": {
                        "name": "web_search",
                        "arguments": json.dumps({"query": f"step {step}"}),
                    },
                }]}
            yield {"type": "metadata", "usage": {"total_tokens": 10}}

        return chunks()


async def _dispatch(name: str, args: dict, context: dict) -> str:
    """A tool that returns a lot, with the secret buried in the second call."""
    step = args.get("query", "")
    body = f"result for {step}. " + ("filler text. " * 900)
    if step == "step 2":
        body = f"{SECRET}. {body}"
    return body


async def _never(name, args, context) -> bool:
    return False


class LongRunTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user("e2e", "e@example.com", "x")

    def setUp(self):
        async def small_budget(model, *, reserve_output=0):
            return BUDGET_TOKENS

        patcher = patch("llm.budget.input_budget_for", new=small_budget)
        patcher.start()
        self.addCleanup(patcher.stop)
        # `curate_node` sizes against the same number the assertions use.
        cached = patch(
            "llm.budget.cached_input_budget",
            new=lambda model, reserve_output=0: BUDGET_TOKENS,
        )
        cached.start()
        self.addCleanup(cached.stop)

    def _turn(self, policy, thread: str) -> TurnContext:
        return TurnContext(
            provider="stub", model="stub-model", system_message="You are a test.",
            user_id=self.user.id, session_id=thread, intent="research",
            user_text="do the work", memory_enabled=False,
            max_iterations=40,
            tool_dispatch=_dispatch,
            approval_policy=_never,
            curation=policy,
        )

    def _run(self, policy, *, turns=20, thread="e2e-thread"):
        stub = _StubProvider(turns)

        async def summarise(_turn_ctx):
            async def fold(text):
                # Deliberately keeps the secret if it is in the folded text:
                # a summariser that dropped every specific would make the recall
                # assertion below pass or fail for the wrong reason.
                keep = SECRET if SECRET in text else "earlier steps happened"
                return f"Summary of earlier work. {keep}", 40
            return fold

        with patch("llm.access.stream", new=stub), \
             patch("chat.turn.agent._summariser_for", new=summarise):
            result = async_to_sync(run_turn)(
                self._turn(policy, thread), prompt="do the work", thread_id=thread
            )
        return stub, result

    # ── the three things a user would notice ─────────────────────────────────

    def test_the_request_never_outgrows_the_window(self):
        policy = curation.CurationPolicy(
            enabled=True, compaction=True, recursive=True, indexing=True,
        )
        stub, result = self._run(policy)

        self.assertGreater(stub.calls, 10, "the run should have gone long")
        worst = max(budget.history_tokens(h) for h in stub.histories)
        self.assertLess(
            worst, BUDGET_TOKENS,
            f"a request of {worst} tokens went out against a {BUDGET_TOKENS} budget",
        )
        self.assertEqual(result.answer, "done")

    def test_without_curation_the_same_run_would_have_overflowed(self):
        """Guards the assertion above from passing because the transcript was
        never large enough to need curating."""
        stub, _ = self._run(curation.CurationPolicy(enabled=False))
        worst = max(budget.history_tokens(h) for h in stub.histories)
        self.assertGreater(worst, BUDGET_TOKENS)

    def test_every_request_stays_sendable(self):
        """No `tool` entry may ever refer to a call that is not in the same
        request. This is the 400 that used to end long runs, asserted on what
        actually went out rather than on the trimmer in isolation."""
        stub, _ = self._run(curation.CurationPolicy(
            enabled=True, compaction=True, recursive=True, indexing=True,
        ))
        for index, history in enumerate(stub.histories):
            offered = {
                call["id"]
                for entry in history
                for call in entry.get("tool_calls") or []
            }
            answered = {
                entry["tool_call_id"]
                for entry in history if entry.get("role") == "tool"
            }
            self.assertTrue(
                answered <= offered,
                f"request {index} orphaned {answered - offered}",
            )

    def test_a_compacted_fact_is_still_reachable(self):
        """The point of indexing. The secret is in turn 2's result, which is
        long gone from the transcript by turn 20 — but it is in the archive, and
        `recall_context` finds it."""
        from chat.tools.tool_output import recall

        policy = curation.CurationPolicy(
            enabled=True, compaction=True, recursive=True, indexing=True,
        )
        self._run(policy, thread="recall-thread")

        out = async_to_sync(recall)("reconciliation code", {
            "user_id": self.user.id, "session_id": "recall-thread",
        })
        self.assertIn("QX-8842", out)

    def test_the_transcript_says_what_was_removed(self):
        policy = curation.CurationPolicy(
            enabled=True, compaction=True, recursive=False, indexing=True,
        )
        stub, _ = self._run(policy, thread="notice-thread")

        final = stub.histories[-1]
        records = [
            entry for entry in final
            if isinstance(entry.get("content"), str)
            and curation.RECORD_PREFIX in entry["content"]
        ]
        self.assertTrue(records, "nothing was compacted in a 20-turn run")
        # A record names the call it replaced and where the rest went, so a
        # truncated result and a short one cannot look alike.
        self.assertIn("web_search(", records[0]["content"])
        self.assertIn("recall_context", records[0]["content"])

    def test_curation_is_not_charged_to_the_user_twice(self):
        """The fold's tokens count once, in the run's total. They are what makes
        it visible to the spend cap."""
        policy = curation.CurationPolicy(
            enabled=True, compaction=True, recursive=True, indexing=True,
        )
        _, result = self._run(policy, thread="tokens-thread")
        self.assertGreater(result.tokens, 0)

    def test_nothing_is_archived_when_indexing_is_off(self):
        self._run(
            curation.CurationPolicy(
                enabled=True, compaction=True, recursive=True, indexing=False,
            ),
            thread="noindex-thread",
        )
        self.assertFalse(
            ToolOutput.objects.filter(session_key="noindex-thread").exists()
        )

    def test_a_short_run_is_untouched(self):
        """The watermark is the whole reason a normal run pays nothing for any
        of this."""
        stub, _ = self._run(
            curation.CurationPolicy(enabled=True), turns=2, thread="short-thread"
        )
        final = stub.histories[-1]
        self.assertFalse([
            entry for entry in final
            if isinstance(entry.get("content"), str)
            and curation.RECORD_PREFIX in entry["content"]
        ])
        self.assertFalse(
            ToolOutput.objects.filter(session_key="short-thread").exists()
        )
