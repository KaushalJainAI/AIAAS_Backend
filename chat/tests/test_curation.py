"""
Context curation: what a long run drops, and what it can get back.

Grouped by the mistake each test pins rather than by module, in the style of
`agents/tests/test_regressions.py` — the failures being guarded against here are
all "the transcript changed shape and nobody noticed", and they show up in three
different files.
"""
from __future__ import annotations

import json
from unittest.mock import patch

from asgiref.sync import async_to_sync
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from langchain_core.messages import (
    AIMessage,
    HumanMessage,
    RemoveMessage,
    SystemMessage,
    ToolMessage,
)

from chat.models import ToolOutput
from chat.turn import curation
from chat.turn.agent import to_wire
from llm import budget
from llm.access import clamp_input

User = get_user_model()


def _tool_turn(call_id: str, name: str = "web_search", result: str = "result",
               *, prefix: str = "m") -> list:
    """One assistant tool-call turn plus the result answering it."""
    return [
        AIMessage(
            content="",
            tool_calls=[{"name": name, "args": {"query": "q"}, "id": call_id}],
            id=f"{prefix}-{call_id}-call",
        ),
        ToolMessage(content=result, tool_call_id=call_id, name=name,
                    id=f"{prefix}-{call_id}-result"),
    ]


# ── The 400 that trimming used to cause ──────────────────────────────────────

class OrphanedToolResultTests(TestCase):
    """`clamp_input` popped one message at a time, so it routinely dropped an
    assistant turn and kept the `tool` messages answering it. Every
    OpenAI-compatible provider rejects a `tool_call_id` that refers to no call
    in the request, so an overlong run did not degrade — it 400'd."""

    def test_trimming_never_leaves_a_result_without_its_call(self):
        history: list[dict] = []
        for index in range(40):
            history.append({
                "role": "assistant",
                "content": None,
                "tool_calls": [{
                    "id": f"call-{index}",
                    "type": "function",
                    "function": {"name": "web_search",
                                 "arguments": json.dumps({"query": "x" * 200})},
                }],
            })
            history.append({
                "role": "tool",
                "tool_call_id": f"call-{index}",
                "content": "y" * 40_000,
            })

        _, _, trimmed = clamp_input("question", "system", history, 20_000)

        offered = {
            call["id"]
            for entry in trimmed
            for call in entry.get("tool_calls") or []
        }
        answered = {
            entry["tool_call_id"] for entry in trimmed if entry.get("role") == "tool"
        }
        self.assertTrue(answered <= offered,
                        f"orphaned tool results: {answered - offered}")

    def test_something_was_actually_dropped(self):
        """Guards the test above from passing vacuously: if nothing is ever
        trimmed, "no orphans" is true and meaningless."""
        history = []
        for index in range(40):
            history.extend([
                {"role": "assistant", "content": None, "tool_calls": [{
                    "id": f"call-{index}", "type": "function",
                    "function": {"name": "t", "arguments": "{}"}}]},
                {"role": "tool", "tool_call_id": f"call-{index}",
                 "content": "y" * 40_000},
            ])

        _, _, trimmed = clamp_input("question", "system", history, 20_000)
        self.assertLess(len(trimmed), len(history))

    def test_tool_call_arguments_are_counted(self):
        """`content` is None on a tool-calling turn and the payload lives in
        `arguments`, so counting only content scored the largest entries in an
        agent transcript at zero."""
        entry = {
            "role": "assistant",
            "content": None,
            "tool_calls": [{
                "id": "c1", "type": "function",
                "function": {"name": "execute_python",
                             "arguments": json.dumps({"code": "x" * 8_000})},
            }],
        }
        self.assertGreater(budget.entry_tokens(entry), 1_500)


# ── Segments ─────────────────────────────────────────────────────────────────

class SegmentTests(TestCase):
    def test_a_tool_turn_and_its_results_are_one_segment(self):
        messages = [
            HumanMessage(content="go", id="m1"),
            *_tool_turn("c1"),
            *_tool_turn("c2"),
        ]
        grouped = curation.segments(messages)
        self.assertEqual([len(s.messages) for s in grouped], [1, 2, 2])
        self.assertTrue(grouped[1].is_tool_turn)

    def test_two_results_for_one_turn_stay_together(self):
        turn = AIMessage(
            content="",
            tool_calls=[
                {"name": "a", "args": {}, "id": "c1"},
                {"name": "b", "args": {}, "id": "c2"},
            ],
            id="m-turn",
        )
        messages = [
            turn,
            ToolMessage(content="1", tool_call_id="c1", id="r1"),
            ToolMessage(content="2", tool_call_id="c2", id="r2"),
        ]
        self.assertEqual(len(curation.segments(messages)), 1)

    def test_an_orphan_result_attaches_rather_than_standing_alone(self):
        messages = [
            HumanMessage(content="go", id="m1"),
            ToolMessage(content="stray", tool_call_id="gone", id="r1"),
        ]
        grouped = curation.segments(messages)
        self.assertEqual(len(grouped), 1)
        self.assertEqual(len(grouped[0].messages), 2)


# ── The pass ─────────────────────────────────────────────────────────────────

class CurationPolicyTests(TestCase):
    def test_chat_default_is_off(self):
        self.assertFalse(curation.CurationPolicy().enabled)

    def test_all_three_off_disables_the_pass(self):
        policy = curation.CurationPolicy.from_settings(
            {"compaction": False, "recursiveContext": False, "indexing": False}
        )
        self.assertFalse(policy.enabled)

    def test_missing_settings_match_the_serializer_defaults(self):
        policy = curation.CurationPolicy.from_settings({})
        self.assertTrue(policy.enabled)
        self.assertTrue(policy.compaction)
        self.assertTrue(policy.recursive)
        self.assertTrue(policy.indexing)


class CurateTests(TestCase):
    """The pass itself, driven with a tiny budget so a short transcript counts
    as overlong. Sizing is what triggers curation, so shrinking the budget and
    growing the transcript are the same test."""

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(
            username="curator", email="c@example.com", password="x"
        )

    def setUp(self):
        self.context = {
            "user_id": self.user.id,
            "session_id": "agent-1-abc",
            "turn_id": "t1",
        }
        # `input_budget_for` reads AIModel; patch it so these tests are about
        # curation rather than about the registry's contents.
        patcher = patch(
            "llm.budget.input_budget_for", new=self._budget
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    @staticmethod
    async def _budget(model, *, reserve_output=0):
        return 4_000

    async def _summarise(self, text):
        self.summarised = text
        return "Earlier: searched twice, found the figure 4.8.", 120

    def test_short_transcript_is_left_alone(self):
        messages = [HumanMessage(content="hello", id="m1")]
        result = self._run(messages)
        self.assertFalse(result.curated)
        self.assertEqual(result.updates, [])

    def test_compaction_shrinks_old_results_and_keeps_the_call(self):
        messages = [
            HumanMessage(content="go", id="m1"),
            *_tool_turn("c1", result="A" * 30_000, prefix="a"),
            *_tool_turn("c2", result="B" * 30_000, prefix="b"),
            *_tool_turn("c3", result="C" * 100, prefix="c"),
            *_tool_turn("c4", result="D" * 100, prefix="d"),
            *_tool_turn("c5", result="E" * 100, prefix="e"),
        ]
        result = self._run(messages, policy=curation.CurationPolicy(
            enabled=True, compaction=True, recursive=False, indexing=True,
        ))

        self.assertTrue(result.curated)
        self.assertGreaterEqual(result.results_compacted, 1)
        replaced = [m for m in result.updates if isinstance(m, ToolMessage)]
        self.assertTrue(replaced)
        for message in replaced:
            self.assertTrue(message.content.startswith(curation.RECORD_PREFIX))
            # The record names the call it answers, so the transcript still says
            # what was done even though it no longer says what came back.
            self.assertIn("web_search(", message.content)
            # Same id, or `add_messages` would append a second result for a call
            # that already has one.
            self.assertTrue(message.id)

    def test_the_three_most_recent_segments_are_never_touched(self):
        messages = [
            HumanMessage(content="go", id="m1"),
            *_tool_turn("c1", result="A" * 30_000, prefix="a"),
            *_tool_turn("c2", result="B" * 30_000, prefix="b"),
            *_tool_turn("c3", result="C" * 30_000, prefix="c"),
        ]
        result = self._run(messages, policy=curation.CurationPolicy(
            enabled=True, compaction=True, recursive=False, indexing=False,
        ))
        touched = {m.id for m in result.updates}
        self.assertNotIn("c-c3-result", touched)

    def test_indexing_on_archives_what_it_removes(self):
        messages = [
            HumanMessage(content="go", id="m1"),
            *_tool_turn("c1", result="A" * 30_000, prefix="a"),
            *_tool_turn("c2", result="B" * 100, prefix="b"),
            *_tool_turn("c3", result="C" * 100, prefix="c"),
            *_tool_turn("c4", result="D" * 100, prefix="d"),
        ]
        result = self._run(messages, policy=curation.CurationPolicy(
            enabled=True, compaction=True, recursive=False, indexing=True,
        ))
        self.assertTrue(result.archived_ids)
        row = ToolOutput.objects.get(id=result.archived_ids[0])
        self.assertEqual(row.content, "A" * 30_000)
        self.assertEqual(row.session_key, "agent-1-abc")
        record = next(m for m in result.updates if isinstance(m, ToolMessage))
        self.assertIn(row.id, record.content)

    def test_indexing_off_says_the_text_is_gone(self):
        messages = [
            HumanMessage(content="go", id="m1"),
            *_tool_turn("c1", result="A" * 30_000, prefix="a"),
            *_tool_turn("c2", result="B" * 100, prefix="b"),
            *_tool_turn("c3", result="C" * 100, prefix="c"),
            *_tool_turn("c4", result="D" * 100, prefix="d"),
        ]
        result = self._run(messages, policy=curation.CurationPolicy(
            enabled=True, compaction=True, recursive=False, indexing=False,
        ))
        self.assertEqual(ToolOutput.objects.count(), 0)
        record = next(m for m in result.updates if isinstance(m, ToolMessage))
        # A notice pointing at an id nobody wrote is worse than one admitting
        # the text is unrecoverable.
        self.assertIn("not archived", record.content)

    def test_folding_removes_by_id_and_leaves_one_note(self):
        messages = [HumanMessage(content="go", id="m1")]
        for index in range(6):
            messages.extend(
                _tool_turn(f"c{index}", result="Z" * 4_000, prefix=f"p{index}")
            )

        result = self._run(messages, policy=curation.CurationPolicy(
            enabled=True, compaction=False, recursive=True, indexing=True,
        ))

        self.assertTrue(result.steps_folded)
        removals = [m for m in result.updates if isinstance(m, RemoveMessage)]
        notes = [m for m in result.updates if isinstance(m, SystemMessage)]
        self.assertTrue(removals)
        self.assertEqual(len(notes), 1, "a fold must leave exactly one note")
        self.assertTrue(notes[0].content.startswith(curation.SUMMARY_PREFIX))
        self.assertEqual(result.summary_tokens, 120)

    def test_a_previous_note_is_absorbed_rather_than_accumulated(self):
        note = SystemMessage(
            content=curation.SUMMARY_PREFIX + "\nearlier things happened",
            id="note-1",
        )
        messages = [HumanMessage(content="go", id="m1"), note]
        for index in range(6):
            messages.extend(
                _tool_turn(f"c{index}", result="Z" * 4_000, prefix=f"p{index}")
            )

        result = self._run(messages, policy=curation.CurationPolicy(
            enabled=True, compaction=False, recursive=True, indexing=False,
        ))

        removed = {m.id for m in result.updates if isinstance(m, RemoveMessage)}
        self.assertIn("note-1", removed)
        self.assertIn("earlier things happened", self.summarised)

    def test_a_failed_fold_changes_nothing(self):
        messages = [HumanMessage(content="go", id="m1")]
        for index in range(6):
            messages.extend(
                _tool_turn(f"c{index}", result="Z" * 4_000, prefix=f"p{index}")
            )

        async def explode(_text):
            raise RuntimeError("provider down")

        result = self._run(
            messages,
            policy=curation.CurationPolicy(
                enabled=True, compaction=False, recursive=True, indexing=False,
            ),
            summarise=explode,
        )
        self.assertEqual(result.steps_folded, 0)
        self.assertFalse([m for m in result.updates if isinstance(m, RemoveMessage)])

    def test_a_second_pass_does_not_re_archive(self):
        """Compaction is applied to state, so the next pass sees records rather
        than the original text. Re-archiving would mean a row per turn per
        result for the rest of the run."""
        messages = [
            HumanMessage(content="go", id="m1"),
            *_tool_turn("c1", result="A" * 30_000, prefix="a"),
            *_tool_turn("c2", result="B" * 100, prefix="b"),
            *_tool_turn("c3", result="C" * 100, prefix="c"),
            *_tool_turn("c4", result="D" * 100, prefix="d"),
        ]
        policy = curation.CurationPolicy(
            enabled=True, compaction=True, recursive=False, indexing=True,
        )
        first = self._run(messages, policy=policy)
        self.assertEqual(ToolOutput.objects.count(), 1)

        # Apply the updates the way `add_messages` would, then curate again.
        by_id = {m.id: m for m in first.updates}
        applied = [by_id.get(m.id, m) for m in messages]
        self._run(applied, policy=policy)
        self.assertEqual(ToolOutput.objects.count(), 1)

    # ── driver ───────────────────────────────────────────────────────────────

    def _run(self, messages, *, policy=None, summarise=None):
        policy = policy or curation.CurationPolicy(enabled=True)
        return async_to_sync(curation.curate)(
            messages,
            policy=policy,
            model="test-model",
            reserve_output=1_000,
            context=self.context,
            summarise=summarise or self._summarise,
        )


# ── Recall ───────────────────────────────────────────────────────────────────

class RecallTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(
            username="recaller", email="r@example.com", password="x"
        )
        cls.other = User.objects.create_user(
            username="stranger", email="s@example.com", password="x"
        )

    def setUp(self):
        self.context = {"user_id": self.user.id, "session_id": "run-1"}
        ToolOutput.objects.create(
            user=self.user, session_key="run-1", tool_name="context:archive:web_search",
            content="The quarterly revenue figure was 4.8 million rupees.",
            total_chars=52,
            expires_at=timezone.now() + timezone.timedelta(hours=1),
        )

    def _recall(self, query, context=None):
        from chat.tools.tool_output import recall

        return async_to_sync(recall)(query, context or self.context)

    def test_a_match_returns_the_text_and_a_readable_id(self):
        out = self._recall("revenue figure")
        self.assertIn("4.8 million", out)
        self.assertIn("read_tool_output", out)

    def test_another_users_archive_is_not_reachable(self):
        out = self._recall(
            "revenue", {"user_id": self.other.id, "session_id": "run-1"}
        )
        self.assertIn("Nothing has been archived", out)

    def test_another_run_is_not_reachable(self):
        out = self._recall(
            "revenue", {"user_id": self.user.id, "session_id": "run-2"}
        )
        self.assertIn("Nothing has been archived", out)

    def test_no_match_says_so_rather_than_returning_something_irrelevant(self):
        out = self._recall("kangaroo unrelated")
        self.assertIn("No archived text", out)


# ── The note has to reach the model ──────────────────────────────────────────

class NoteReachesTheWireTests(TestCase):
    def test_to_wire_renders_the_summary_note(self):
        """`to_wire` matched Human/AI/Tool and silently dropped everything else,
        so a note added to state would never have been sent — the run would look
        to the model exactly like one that forgot its first thirty steps."""
        wire = to_wire([
            SystemMessage(content=curation.SUMMARY_PREFIX + "\nwhat happened"),
            HumanMessage(content="carry on"),
        ])
        self.assertEqual(wire[0]["role"], "system")
        self.assertIn("what happened", wire[0]["content"])


# ── The node ─────────────────────────────────────────────────────────────────

class CurateNodeTests(TestCase):
    """The graph node: it must be invisible to chat, silent when there is
    nothing to do, and incapable of failing a run."""

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(
            username="noder", email="n@example.com", password="x"
        )

    def setUp(self):
        patcher = patch("llm.budget.input_budget_for", new=self._budget)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.observed = []

    @staticmethod
    async def _budget(model, *, reserve_output=0):
        return 4_000

    def _state(self, messages):
        return {
            "messages": messages, "metadata": {}, "tool_trace": [],
            "thinking": "", "total_tokens": 500,
        }

    def _turn(self, policy):
        from chat.turn.agent import TurnContext

        async def observe(**kwargs):
            self.observed.append(kwargs)

        return TurnContext(
            provider="test", model="test-model", system_message="sys",
            user_id=self.user.id, session_id="agent-9-xyz", intent="research",
            user_text="goal", curation=policy, on_curation=observe,
        )

    def _run(self, messages, policy):
        from chat.turn.agent import curate_node

        return async_to_sync(curate_node)(
            self._state(messages), {"configurable": {"turn": self._turn(policy)}}
        )

    def _long(self):
        messages = [HumanMessage(content="go", id="m1")]
        for index in range(6):
            messages.extend(
                _tool_turn(f"c{index}", result="Z" * 20_000, prefix=f"p{index}")
            )
        return messages

    def test_chat_is_untouched(self):
        """`curation=None` is what every chat turn passes. The node must not
        read state, hit the database, or return an update."""
        self.assertEqual(self._run(self._long(), None), {})
        self.assertEqual(self.observed, [])

    def test_a_disabled_policy_does_nothing(self):
        policy = curation.CurationPolicy(enabled=False)
        self.assertEqual(self._run(self._long(), policy), {})

    def test_an_enabled_policy_cuts_and_reports(self):
        policy = curation.CurationPolicy(
            enabled=True, compaction=True, recursive=False, indexing=True,
        )
        out = self._run(self._long(), policy)

        self.assertTrue(out["messages"])
        self.assertEqual(len(self.observed), 1)
        self.assertGreater(self.observed[0]["tokens_before"],
                           self.observed[0]["tokens_after"])

    def test_the_fold_is_charged_to_the_run(self):
        """A summariser that spent money invisibly would be a hole in the spend
        cap it is meant to serve."""
        async def summariser(_turn):
            async def summarise(_text):
                return "earlier: several searches", 400
            return summarise

        policy = curation.CurationPolicy(
            enabled=True, compaction=False, recursive=True, indexing=False,
        )
        with patch("chat.turn.agent._summariser_for", new=summariser):
            out = self._run(self._long(), policy)

        self.assertEqual(out["total_tokens"], 900)  # 500 already spent + 400

    def test_a_broken_curator_does_not_fail_the_run(self):
        policy = curation.CurationPolicy(enabled=True)
        with patch("chat.turn.curation.curate", side_effect=RuntimeError("boom")):
            self.assertEqual(self._run(self._long(), policy), {})


class SummaryModelChoiceTests(TestCase):
    """Which model folds a run: the agent's choice, then the platform's, then
    the run's own. Each step is a narrower statement of intent than the next."""

    def test_platform_default_is_a_model_the_platform_holds_a_key_for(self):
        """The fold has to work for a user who has connected nothing, or it
        silently stops folding and long runs go back to losing their oldest
        steps with no summary behind them."""
        from django.conf import settings
        from credentials.resolution import PLATFORM_ENV_KEYS

        self.assertIn(settings.CONTEXT_SUMMARY_PROVIDER, PLATFORM_ENV_KEYS)

    def test_an_agent_can_choose_its_own(self):
        policy = curation.CurationPolicy.from_settings({
            "summaryModel": "openai/gpt-4o-mini",
            "summaryProvider": "openrouter",
        })
        self.assertEqual(policy.summary_model, "openai/gpt-4o-mini")
        self.assertEqual(policy.summary_provider, "openrouter")

    def test_blank_falls_back_to_the_platform_default(self):
        from django.conf import settings

        policy = curation.CurationPolicy.from_settings(
            {"summaryModel": "", "summaryProvider": ""}
        )
        self.assertEqual(policy.summary_model, settings.CONTEXT_SUMMARY_MODEL)
        self.assertEqual(policy.summary_provider, settings.CONTEXT_SUMMARY_PROVIDER)

    def test_the_runs_own_model_is_the_last_resort(self):
        """`_summariser_for` falls back to the run's model when the policy names
        none — and again, at call time, when the named one cannot be used."""
        from chat.turn.agent import TurnContext, _summariser_for

        turn = TurnContext(
            provider="nvidia", model="run-model", system_message="s", user_id=1,
            session_id="s1", intent="research", user_text="g",
            curation=curation.CurationPolicy(enabled=True),
        )
        captured = {}

        async def fake_complete(**kwargs):
            captured.update(kwargs)
            from llm.access import Completion
            return Completion(content="note", tokens=7)

        with patch("llm.access.complete", new=fake_complete):
            summarise = async_to_sync(_summariser_for)(turn)
            self.assertEqual(async_to_sync(summarise)("text"), ("note", 7))

        self.assertEqual(captured["model"], "run-model")

    def test_an_unusable_fold_model_falls_back_rather_than_losing_the_note(self):
        from chat.turn.agent import TurnContext, _summariser_for
        from llm.access import Completion, LLMNoCredential

        turn = TurnContext(
            provider="nvidia", model="run-model", system_message="s", user_id=1,
            session_id="s1", intent="research", user_text="g",
            curation=curation.CurationPolicy(
                enabled=True, summary_provider="openrouter",
                summary_model="pinned-model",
            ),
        )
        tried = []

        async def fake_complete(**kwargs):
            tried.append(kwargs["model"])
            if kwargs["model"] == "pinned-model":
                raise LLMNoCredential("no key")
            return Completion(content="note", tokens=3)

        with patch("llm.access.complete", new=fake_complete):
            summarise = async_to_sync(_summariser_for)(turn)
            content, tokens = async_to_sync(summarise)("text")

        self.assertEqual(tried, ["pinned-model", "run-model"])
        self.assertEqual((content, tokens), ("note", 3))
