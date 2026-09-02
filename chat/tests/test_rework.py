"""
Tests for the reworked chat context/attachment/artifact behaviour.

The theme across these: each one pins a decision where the *wrong* behaviour is
silent. A context overflow shows up as a provider 400 long after the fact; an
image sent to a text-only model gets dropped and answered around; an unclamped
artifact only misbehaves once it is replayed from history. None of these
announce themselves, so they get tests.
"""
from __future__ import annotations

import json

from asgiref.sync import async_to_sync
from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from chat.turn import agent, prompts

from chat.models import ChatSession, ChatMessage
from chat.tools import AVAILABLE_TOOLS, execute_tool, get_available_tools
from chat.tools.artifacts import render_html_artifact
from chat.tools.conversation import search_conversation_history
from chat.turn.history import describe_blocked as describe_blocked_attachments
from llm.access import (
    clamp_input as clamp_llm_input,
    estimate_tokens,
    truncate_middle as _truncate_middle,
)
from chat.turn.pipeline import looks_like_recall as looks_like_conversation_recall
from workflow_backend.thresholds import (
    HISTORY_SEARCH_MAX_TOTAL_CHARS,
    HTML_ARTIFACT_MAX_HEIGHT,
    HTML_ARTIFACT_MAX_WIDTH,
    HTML_ARTIFACT_MIN_WIDTH,
    MAX_LLM_INPUT_TOKENS,
    MAX_SINGLE_MESSAGE_TOKENS,
)


# ─────────────────────────────────────────────────────────────────────────
# Input clamping
# ─────────────────────────────────────────────────────────────────────────

class TruncateMiddleTests(SimpleTestCase):
    def test_short_text_untouched(self):
        self.assertEqual(_truncate_middle("hello", 100), "hello")

    def test_keeps_both_ends(self):
        # The tail matters: it is usually where the actual question lives.
        text = "HEAD" + ("x" * 100_000) + "TAIL"
        out = _truncate_middle(text, 1_000)
        self.assertTrue(out.startswith("HEAD"))
        self.assertTrue(out.endswith("TAIL"))
        self.assertLess(len(out), len(text))
        self.assertIn("trimmed", out)


class ClampLlmInputTests(SimpleTestCase):
    def test_small_input_passes_through_unchanged(self):
        prompt, system, history = clamp_llm_input(
            "hi", "be nice", [{"role": "user", "content": "earlier"}]
        )
        self.assertEqual(prompt, "hi")
        self.assertEqual(system, "be nice")
        self.assertEqual(len(history), 1)

    def test_total_is_bounded_even_when_each_part_is_legal(self):
        # 40 messages that are individually fine but collectively are not. This
        # is the case the per-section budgets miss, because each is computed in
        # isolation.
        big = "word " * 40_000  # ~50k tokens each
        history = [{"role": "user", "content": big} for _ in range(40)]
        prompt, system, out = clamp_llm_input("question", "sys", history)

        total = (
            estimate_tokens(prompt)
            + estimate_tokens(system)
            + sum(estimate_tokens(m["content"]) for m in out)
        )
        self.assertLessEqual(total, MAX_LLM_INPUT_TOKENS)

    def test_oldest_history_is_dropped_first(self):
        big = "word " * 40_000
        history = (
            [{"role": "user", "content": "OLDEST " + big}]
            + [{"role": "user", "content": "MIDDLE " + big} for _ in range(10)]
            + [{"role": "user", "content": "NEWEST " + big}]
        )
        _, _, out = clamp_llm_input("q", "s", history)
        kept = " ".join(m["content"][:20] for m in out)
        self.assertIn("NEWEST", kept)
        self.assertNotIn("OLDEST", kept)

    def test_dropping_history_leaves_a_notice_for_the_model(self):
        # Without this the model cannot tell "never happened" from "trimmed",
        # and answers from an incomplete record instead of searching.
        big = "word " * 40_000
        history = [{"role": "user", "content": big} for _ in range(40)]
        _, _, out = clamp_llm_input("q", "s", history)
        self.assertTrue(out)
        self.assertEqual(out[0]["role"], "system")
        self.assertIn("search_conversation_history", out[0]["content"])

    def test_single_giant_message_is_truncated_not_dropped(self):
        giant = "z" * (MAX_SINGLE_MESSAGE_TOKENS * 4 * 3)
        _, _, out = clamp_llm_input("q", "s", [{"role": "user", "content": giant}])
        self.assertEqual(len(out), 1)
        self.assertLessEqual(estimate_tokens(out[0]["content"]), MAX_SINGLE_MESSAGE_TOKENS + 50)

    def test_prompt_survives_when_history_is_empty(self):
        prompt, _, out = clamp_llm_input("the actual question", "s", [])
        self.assertEqual(prompt, "the actual question")
        self.assertEqual(out, [])

    def test_none_history_is_accepted(self):
        prompt, _, out = clamp_llm_input("q", "s", None)
        self.assertEqual(out, [])


# ─────────────────────────────────────────────────────────────────────────
# Attachment capability gating
# ─────────────────────────────────────────────────────────────────────────

class BlockedAttachmentMessageTests(SimpleTestCase):
    def test_empty_is_empty(self):
        self.assertEqual(describe_blocked_attachments([]), "")

    def test_switchable_tells_user_to_change_model(self):
        # Still the message when no witness is available: an image nobody can
        # look at is an image nobody can look at, and switching is the only fix.
        msg = describe_blocked_attachments([{
            "filename": "chart.png", "file_type": "image",
            "reason": "x", "switch_model_helps": True,
        }])
        self.assertIn("chart.png", msg)
        self.assertIn("multimodal", msg)

    def test_witness_replaces_the_switch_model_message(self):
        # Inverted deliberately in the vision-agent change (docs/VISION_AGENT.md).
        # With a witness the upload is not a dead end, so telling the user to go
        # and change models would be sending them on an errand for nothing.
        msg = describe_blocked_attachments([{
            "filename": "chart.png", "file_type": "image",
            "attachment_id": "4f2a", "reason": "x",
            "switch_model_helps": True, "witness": True,
        }])
        self.assertIn("chart.png", msg)
        self.assertIn("vision model", msg)
        self.assertNotIn("Switch to a multimodal model", msg)

    def test_unsupported_type_does_not_suggest_switching(self):
        # Switching models will not help for a format nothing can read; saying so
        # would send the user on a pointless hunt.
        msg = describe_blocked_attachments([{
            "filename": "track.mp3", "file_type": "audio",
            "reason": "x", "switch_model_helps": False,
        }])
        self.assertIn("track.mp3", msg)
        self.assertNotIn("Switch to a multimodal model", msg)


# ─────────────────────────────────────────────────────────────────────────
# HTML artifact clamping
# ─────────────────────────────────────────────────────────────────────────

class HtmlArtifactTests(SimpleTestCase):
    def _render(self, **args) -> dict:
        return json.loads(async_to_sync(render_html_artifact)(args, {}))

    def test_oversized_dimensions_are_clamped(self):
        out = self._render(html="<p>hi</p>", width=99999, height=99999)
        self.assertEqual(out["width"], HTML_ARTIFACT_MAX_WIDTH)
        self.assertEqual(out["height"], HTML_ARTIFACT_MAX_HEIGHT)

    def test_absurdly_small_is_raised_to_the_floor(self):
        out = self._render(html="<p>hi</p>", width=1, height=1)
        self.assertEqual(out["width"], HTML_ARTIFACT_MIN_WIDTH)

    def test_negative_dimensions_are_clamped_not_passed_through(self):
        out = self._render(html="<p>hi</p>", width=-500, height=-500)
        self.assertGreaterEqual(out["width"], HTML_ARTIFACT_MIN_WIDTH)
        self.assertGreaterEqual(out["height"], 1)

    def test_garbage_dimensions_fall_back_to_defaults(self):
        out = self._render(html="<p>hi</p>", width="huge", height=None)
        self.assertLessEqual(out["width"], HTML_ARTIFACT_MAX_WIDTH)
        self.assertGreaterEqual(out["width"], HTML_ARTIFACT_MIN_WIDTH)

    def test_missing_html_is_an_error(self):
        self.assertIn("error", self._render(html="   "))

    def test_enormous_payload_is_truncated_and_flagged(self):
        out = self._render(html="<p>" + ("x" * 500_000) + "</p>")
        self.assertTrue(out["truncated"])
        self.assertIn("truncated", out["note"].lower())


# ─────────────────────────────────────────────────────────────────────────
# Conversation history search
# ─────────────────────────────────────────────────────────────────────────

class ConversationHistorySearchTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_user(
            username="searcher", email="s@example.com", password="pw"
        )
        self.other = User.objects.create_user(
            username="stranger", email="o@example.com", password="pw"
        )
        self.session = ChatSession.objects.create(user=self.user, title="T")
        ChatMessage.objects.create(
            session=self.session, role="user",
            content="My account number is 4417 and the invoice is overdue.",
        )
        ChatMessage.objects.create(
            session=self.session, role="assistant",
            content="Noted, I will look into the billing issue.",
            metadata={"thinking": "The user mentioned a pelican in passing earlier."},
        )

    def _search(self, ctx=None, **args):
        ctx = ctx if ctx is not None else {
            "user_id": self.user.id, "session_id": str(self.session.id),
        }
        return json.loads(async_to_sync(search_conversation_history)(args, ctx))

    def test_finds_an_old_message_by_keyword(self):
        out = self._search(query="account number")
        self.assertTrue(out["matches"])
        self.assertIn("4417", out["matches"][0]["snippet"])

    def test_searches_stored_reasoning_too(self):
        out = self._search(query="pelican", scope="reasoning")
        self.assertTrue(out["matches"])
        self.assertEqual(out["matches"][0]["found_in"], "reasoning")

    def test_scope_messages_excludes_reasoning(self):
        out = self._search(query="pelican", scope="messages")
        self.assertEqual(out["matches"], [])

    def test_role_filter(self):
        out = self._search(query="billing issue", role="user")
        self.assertEqual(out["matches"], [])

    def test_no_match_suggests_retrying_rather_than_giving_up(self):
        out = self._search(query="zzzz nonexistent qqqq")
        self.assertEqual(out["matches"], [])
        self.assertIn("different keywords", out["message"])

    def test_other_users_session_is_not_readable(self):
        # Scoping on session_id alone would make a guessed UUID enough.
        out = self._search(
            query="account number",
            ctx={"user_id": self.other.id, "session_id": str(self.session.id)},
        )
        self.assertEqual(out["matches"], [])

    def test_missing_session_context_is_an_error(self):
        out = self._search(query="anything", ctx={"user_id": self.user.id})
        self.assertIn("error", out)

    def test_empty_query_is_rejected(self):
        self.assertIn("error", self._search(query="   "))

    def test_result_size_is_bounded(self):
        # The tool exists to protect the context window, so it must not be able
        # to return more than the window it protects.
        for i in range(60):
            ChatMessage.objects.create(
                session=self.session, role="user",
                content=f"pineapple entry {i} " + ("filler text " * 400),
            )
        out = self._search(query="pineapple")
        total = sum(len(m["snippet"]) for m in out["matches"])
        self.assertLessEqual(total, HISTORY_SEARCH_MAX_TOTAL_CHARS)

    def test_regex_metacharacters_are_treated_literally(self):
        # Term matching, not regex — a catastrophic-backtracking pattern must be
        # inert rather than pinning the worker.
        out = self._search(query="(a+)+$ [z-a] \\")
        self.assertIn("matches", out)


# ─────────────────────────────────────────────────────────────────────────
# Memory toggle
# ─────────────────────────────────────────────────────────────────────────

class MemoryToggleTests(TestCase):
    def test_defaults_to_on(self):
        User = get_user_model()
        user = User.objects.create_user(username="m", email="m@e.com", password="pw")
        self.assertTrue(ChatSession.objects.create(user=user, title="T").memory_enabled)

    def test_memory_dependent_tools_are_withheld_when_off(self):
        tools = async_to_sync(get_available_tools)(None, memory_enabled=False)
        names = {t["function"]["name"] for t in tools}
        self.assertNotIn("search_conversation_history", names)
        self.assertNotIn("get_chat_message_full_text", names)

    def test_memory_dependent_tools_are_present_when_on(self):
        tools = async_to_sync(get_available_tools)(None, memory_enabled=True)
        names = {t["function"]["name"] for t in tools}
        self.assertIn("search_conversation_history", names)


# ─────────────────────────────────────────────────────────────────────────
# Eager conversation recall
# ─────────────────────────────────────────────────────────────────────────

class ConversationRecallDetectionTests(SimpleTestCase):
    RECALL = [
        "What was the project codename I told you earlier?",
        "Remind me what we decided",
        "You said something about the port number",
        "what did I mention about the invoice",
        "go back to the figure I gave you",
        "Do you remember my name?",
    ]
    NOT_RECALL = [
        "What is the capital of France?",
        "Write me a haiku about rain",
        "Explain how TCP congestion control works",
    ]

    def test_recognises_questions_about_the_conversation(self):
        for q in self.RECALL:
            self.assertTrue(looks_like_conversation_recall(q), q)

    def test_leaves_ordinary_questions_alone(self):
        # False positives are cheap (one DB scan) but not free — a general
        # knowledge question should not drag conversation history into context.
        for q in self.NOT_RECALL:
            self.assertFalse(looks_like_conversation_recall(q), q)

    def test_is_case_insensitive(self):
        self.assertTrue(looks_like_conversation_recall("WHAT DID I TELL YOU EARLIER"))

    def test_handles_empty_input(self):
        self.assertFalse(looks_like_conversation_recall(""))
        self.assertFalse(looks_like_conversation_recall(None))


# ─────────────────────────────────────────────────────────────────────────
# Agent loop state handling
# ─────────────────────────────────────────────────────────────────────────

class TranscriptThreadingTests(SimpleTestCase):
    """
    The model must receive tool results as real `tool` messages linked by
    `tool_call_id`, not as prose folded into the prompt. Flattening them is what
    made models imitate tool-call syntax in text instead of emitting it.
    """

    def _transcript(self):
        return [
            HumanMessage(content="FIRST QUESTION"),
            AIMessage(content="first answer"),
            HumanMessage(content="SECOND QUESTION"),
        ]

    def test_latest_human_message_is_the_prompt(self):
        # The checkpointer is keyed by session id and `messages` uses an
        # appending reducer, so on turn 2 the state still holds turn 1's
        # HumanMessage. Taking the *first* one made every later turn re-answer
        # the opening question.
        history, prompt = agent._split_transcript(self._transcript(), at_limit=False)

        self.assertEqual(prompt, "SECOND QUESTION")
        self.assertEqual([m["role"] for m in history], ["user", "assistant"])

    def test_tool_results_are_tool_role_messages(self):
        messages = [
            HumanMessage(content="who won?"),
            AIMessage(content="", tool_calls=[
                {"name": "web_search", "args": {"query": "who won"}, "id": "call_1"},
            ]),
            ToolMessage(content="Team A won.", tool_call_id="call_1", name="web_search"),
        ]

        history, prompt = agent._split_transcript(messages, at_limit=False)

        assistant = history[1]
        self.assertEqual(assistant["role"], "assistant")
        self.assertEqual(assistant["tool_calls"][0]["function"]["name"], "web_search")

        result = history[2]
        self.assertEqual(result["role"], "tool")
        self.assertEqual(result["tool_call_id"], "call_1")
        self.assertEqual(result["content"], "Team A won.")

        # A handler always appends `prompt` as the final user turn, so a tool
        # result can never be last on the wire; the nudge fills that slot.
        self.assertEqual(prompt, prompts.CONTINUE)

    def test_at_limit_prompt_forbids_further_tools(self):
        messages = [
            HumanMessage(content="q"),
            AIMessage(content="", tool_calls=[{"name": "web_search", "args": {}, "id": "c"}]),
            ToolMessage(content="r", tool_call_id="c", name="web_search"),
        ]

        _, prompt = agent._split_transcript(messages, at_limit=True)

        self.assertEqual(prompt, prompts.CONTINUE_AT_LIMIT)


# ─────────────────────────────────────────────────────────────────────────
# Removed capabilities
# ─────────────────────────────────────────────────────────────────────────

class RemovedCapabilityTests(SimpleTestCase):
    """Capabilities that were removed and must not come back by any door.

    `list_files` / `read_file` / `write_file` / `delete_file` used to be on this
    list. They are not any more, and the distinction is the whole point: those
    names reached the *host* filesystem from a chat turn. The tools carrying
    those names now address rows in the caller's own Folder/Document tree
    through `inference/vfs.py`, cannot name a path on any disk, and are
    unreachable from chat at all. `AgentFileToolsAreNotChatToolsTests` pins the
    half of the original decision that is still true.
    """

    REMOVED = [
        "execute_shell", "execute_python_code", "run_workflow", "list_workflows",
        "suggest_workflow",
        # Screen control belongs to the canvas agent. Chat's copies drove the
        # user's UI through an action channel that no longer exists, and
        # `dispatch_ui_actions` spoke a frame no consumer ever handled.
        "frontend_click", "frontend_fill", "frontend_navigate",
        "dispatch_ui_actions",
    ]

    def test_removed_tools_are_not_advertised(self):
        names = {t["function"]["name"] for t in AVAILABLE_TOOLS}
        for gone in self.REMOVED:
            self.assertNotIn(gone, names)

    def test_removed_tools_are_not_dispatchable(self):
        # Advertising is one thing; a live dispatch entry would let a model that
        # remembers the old name still reach the capability.
        for gone in self.REMOVED:
            res = async_to_sync(execute_tool)(gone, {}, {"user_id": 1})
            self.assertIn("not recognized", res)


class AgentFileToolsAreNotChatToolsTests(SimpleTestCase):
    """The file tools exist, and chat cannot reach them.

    This replaced `list_files` et al. sitting in `REMOVED`. The old assertion
    was "these names do not exist"; the true one is "these names are not a chat
    capability" — the host-filesystem tools they used to name are still gone,
    and nothing here brings them back.
    """

    FILE_TOOLS = ["list_files", "read_file", "write_file", "make_directory",
                  "delete_file"]

    def test_not_offered_in_chat(self):
        # `requires="files"` is unmet in chat by construction: a chat turn has
        # no fileAccess setting, so there is no scope to build one from.
        offered = {
            t["function"]["name"]
            for t in async_to_sync(get_available_tools)(None)
        }
        for name in self.FILE_TOOLS:
            self.assertNotIn(name, offered)

    def test_refuse_without_a_scope(self):
        # Reachable through `execute_tool` — they are ordinary registrations —
        # but with no scope in context they decline instead of defaulting to
        # one. A default here would be access nobody granted.
        for name in self.FILE_TOOLS:
            res = async_to_sync(execute_tool)(name, {"path": "x"}, {"user_id": 1})
            self.assertIn("no file access", res.lower())

    def test_reachable_only_through_the_fileOps_grant(self):
        from agents.agent.runtime import GRANT_TOOLS

        self.assertEqual(set(GRANT_TOOLS["fileOps"]), set(self.FILE_TOOLS))
