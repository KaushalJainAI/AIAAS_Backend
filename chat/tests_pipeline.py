"""
End-to-end tests for one chat turn, with the provider faked at the boundary.

These are the tests the old design could not have: with the answer trapped in a
JSON envelope there was nothing to assert about streaming, and with tool results
flattened into prose there was no transcript to check.
"""
from __future__ import annotations

import json
from typing import Any

from asgiref.sync import async_to_sync
from django.contrib.auth import get_user_model
from django.test import TestCase
from unittest.mock import patch

from chat.events import Event
from chat.models import ChatMessage, ChatSession
from chat.pipeline import TurnError, TurnRequest, context_summary, run_chat_turn

User = get_user_model()


class Recorder:
    """An `EventSink` that keeps what it was given."""

    def __init__(self) -> None:
        self.events: list[tuple[Event, dict[str, Any]]] = []

    async def __call__(self, event: Event, payload: dict[str, Any]) -> None:
        self.events.append((event, payload))

    def of(self, event: Event) -> list[dict[str, Any]]:
        return [payload for name, payload in self.events if name == event]

    @property
    def streamed_text(self) -> str:
        return "".join(p.get("content", "") for p in self.of(Event.CONTENT_CHUNK))


def text_stream(*chunks: str):
    """A provider stream that emits `chunks` as content and finishes."""

    async def _stream(**_kwargs):
        for chunk in chunks:
            yield {"type": "content", "content": chunk}
        yield {"type": "metadata", "usage": {"total_tokens": 11}}

    return _stream


def tool_then_answer(tool: str, args: dict, answer: str):
    """First call asks for a tool, second answers. Mirrors a real two-step turn."""
    calls = {"n": 0}

    async def _stream(**_kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            yield {"type": "tool_calls", "tool_calls": [{
                "index": 0, "id": "call_1",
                "function": {"name": tool, "arguments": json.dumps(args)},
            }]}
        else:
            yield {"type": "content", "content": answer}
        yield {"type": "metadata", "usage": {"total_tokens": 5}}

    return _stream


class ChatTurnTests(TestCase):
    def setUp(self) -> None:
        self.user = User.objects.create_user(
            username="tester", email="t@example.com", password="pw"
        )
        self.session = ChatSession.objects.create(user=self.user, title="Test")
        # Follow-ups are a separate provider call; silence it unless under test.
        self._patch("chat.agent.suggest_follow_ups", self._no_follow_ups)
        # The real tool list reaches out to the user's MCP servers. These tests
        # are about the loop, not the catalogue, so keep them hermetic.
        self._patch("chat.tools.get_available_tools", self._builtin_tools)
        # Companion media search would hit DuckDuckGo for real.
        self._patch("chat.search.image_search", self._one_image)
        self._patch("chat.search.video_search", self._one_video)
        # Default-deny on tools, so a test that happens to phrase its prompt as a
        # search ("tell me about X") cannot quietly reach the live internet.
        # Tests that care about tools override this with their own `patch`.
        self._patch("chat.tools.execute_tool", self._inert_tool)

    @staticmethod
    async def _inert_tool(name, args, context) -> str:
        return json.dumps({"type": "search_results", "text": "", "sources": []})

    @staticmethod
    async def _one_image(query, *_args, **_kwargs) -> list[dict]:
        return [{"title": query, "image": "http://img", "url": "http://p", "source": "s"}]

    @staticmethod
    async def _one_video(query, *_args, **_kwargs) -> list[dict]:
        return [{"title": query, "url": "http://vid", "description": "", "duration": "",
                 "publisher": ""}]

    def _patch(self, target: str, replacement) -> None:
        patcher = patch(target, replacement)
        patcher.start()
        self.addCleanup(patcher.stop)

    @staticmethod
    async def _no_follow_ups(*_args, **_kwargs) -> list[str]:
        return []

    @staticmethod
    async def _builtin_tools(*_args, **_kwargs) -> list[dict]:
        from chat.tools import ToolExecutor

        return list(ToolExecutor.AVAILABLE_TOOLS)

    def _run(self, content: str, sink) -> Any:
        return async_to_sync(run_chat_turn)(
            session=self.session,
            user=self.user,
            request=TurnRequest.parse({"content": content}),
            sink=sink,
        )

    # ── Streaming ──

    def test_answer_streams_token_by_token(self):
        # The whole point of dropping the JSON envelope: the user sees the answer
        # as it is produced instead of waiting for a complete parseable blob.
        recorder = Recorder()
        with patch("chat.llm.stream", text_stream("Rust ", "is ", "fast.")):
            outcome = self._run("tell me about rust", recorder)

        self.assertEqual(recorder.streamed_text, "Rust is fast.")
        self.assertEqual(outcome.assistant_message.content, "Rust is fast.")

    def test_answer_is_persisted_as_plain_markdown(self):
        with patch("chat.llm.stream", text_stream("# Title\n\nBody.")):
            outcome = self._run("hi", Recorder())

        stored = ChatMessage.objects.get(id=outcome.assistant_message.id)
        self.assertEqual(stored.content, "# Title\n\nBody.")
        self.assertEqual(stored.role, "assistant")

    def test_user_message_is_saved_before_the_model_runs(self):
        recorder = Recorder()
        with patch("chat.llm.stream", text_stream("ok")):
            outcome = self._run("my question", recorder)

        self.assertEqual(outcome.user_message.content, "my question")
        # The client needs the real id early to reconcile its optimistic message.
        ids = [p.get("user_message_id") for p in recorder.of(Event.STATUS)]
        self.assertIn(outcome.user_message.id, ids)

    # ── Tool loop ──

    def test_tool_results_are_threaded_back_as_tool_messages(self):
        recorder = Recorder()
        captured: list[list[dict]] = []

        original = None

        async def fake_tool(name, args, context):
            return json.dumps({"type": "search_results", "text": "Team A won.",
                               "sources": [{"url": "http://a", "title": "A"}]})

        stream = tool_then_answer("web_search", {"query": "who won"}, "Team A won.")

        async def capturing_stream(**kwargs):
            captured.append(list(kwargs.get("history") or []))
            async for chunk in stream(**kwargs):
                yield chunk

        with patch("chat.llm.stream", capturing_stream), \
             patch("chat.tools.execute_tool", fake_tool):
            outcome = self._run("who won the match", recorder)

        # Second call must carry the assistant tool_calls turn and its `tool`
        # result, linked by id — not a prose summary of what happened.
        second = captured[1]
        assistant = next(m for m in second if m["role"] == "assistant")
        result = next(m for m in second if m["role"] == "tool")

        self.assertEqual(assistant["tool_calls"][0]["function"]["name"], "web_search")
        self.assertEqual(result["tool_call_id"],
                         assistant["tool_calls"][0]["id"])
        self.assertEqual(result["content"], fake_result_text())
        self.assertEqual(outcome.assistant_message.content, "Team A won.")

    def test_sources_reach_the_client_and_the_stored_message(self):
        recorder = Recorder()

        async def fake_tool(name, args, context):
            return json.dumps({
                "type": "search_results", "text": "...",
                "sources": [{"url": "http://a", "title": "A"}],
            })

        with patch("chat.llm.stream",
                   tool_then_answer("web_search", {"query": "q"}, "Answer.")), \
             patch("chat.tools.execute_tool", fake_tool):
            outcome = self._run("search something", recorder)

        self.assertEqual(recorder.of(Event.SOURCES_UPDATE)[-1]["sources"][0]["url"],
                         "http://a")
        self.assertEqual(outcome.assistant_message.metadata["sources"][0]["url"],
                         "http://a")

    def test_tool_preamble_is_retracted_from_the_live_buffer(self):
        # A model that says "let me look that up" before calling a tool must not
        # leave that text sitting in the UI as though it were the answer.
        async def preamble_then_tool(**_kwargs):
            yield {"type": "content", "content": "Let me search."}
            yield {"type": "tool_calls", "tool_calls": [{
                "index": 0, "id": "c1",
                "function": {"name": "get_current_time", "arguments": "{}"},
            }]}

        calls = {"n": 0}

        async def stream(**kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                async for chunk in preamble_then_tool(**kwargs):
                    yield chunk
            else:
                yield {"type": "content", "content": "It is noon."}

        async def fake_tool(*_a, **_k):
            return "noon"

        recorder = Recorder()
        with patch("chat.llm.stream", stream), \
             patch("chat.tools.execute_tool", fake_tool):
            outcome = self._run("what time is it", recorder)

        self.assertEqual(len(recorder.of(Event.CONTENT_RESET)), 1)
        self.assertEqual(outcome.assistant_message.content, "It is noon.")

    # ── Explicit intents run their tool ──

    def test_slash_search_always_searches(self):
        # The user picking /search *is* the instruction to search. Leaving it to
        # the model means an explicit search request can come back sourceless.
        seen: list[str] = []

        async def fake_tool(name, args, context):
            seen.append(name)
            return json.dumps({"type": "search_results", "text": "Found it.",
                               "sources": [{"url": "http://a", "title": "A"}]})

        recorder = Recorder()
        with patch("chat.llm.stream", text_stream("Answer.")), \
             patch("chat.tools.execute_tool", fake_tool):
            outcome = self._run("/search rust release date", recorder)

        self.assertIn("web_search", seen)
        self.assertTrue(outcome.assistant_message.metadata["sources"])
        # And the seeded call is visible in the trace, not silently absent.
        self.assertEqual(outcome.assistant_message.metadata["tool_trace"][0]["tool"],
                         "web_search")

    def test_slash_research_runs_deep_research(self):
        seen: list[str] = []

        async def fake_tool(name, args, context):
            seen.append(name)
            return json.dumps({"type": "deep_research", "text": "Corpus.",
                               "queries": ["q"], "sources": [{"url": "http://a"}]})

        with patch("chat.llm.stream", text_stream("Answer.")), \
             patch("chat.tools.execute_tool", fake_tool):
            self._run("/research rust adoption", Recorder())

        self.assertIn("deep_research", seen)

    def test_web_search_also_fills_the_image_panel(self):
        # A Perplexity-style answer with an empty visual panel reads as broken.
        async def fake_tool(name, args, context):
            return json.dumps({"type": "search_results", "text": "t",
                               "sources": [{"url": "http://a", "title": "A"}]})

        recorder = Recorder()
        with patch("chat.llm.stream", text_stream("Answer.")), \
             patch("chat.tools.execute_tool", fake_tool):
            outcome = self._run("/search kittens", recorder)

        self.assertTrue(recorder.of(Event.IMAGES_UPDATE))
        self.assertTrue(outcome.assistant_message.metadata["images"])

    def test_a_recall_question_is_not_a_web_search(self):
        # "what is my name" matches the "what is" search opener but is a
        # question about this conversation. Searching the web for it is both
        # wrong and, now that search intent is seeded, expensive.
        from chat.pipeline import classify_intent

        for question in ("what is my name", "what did i say earlier",
                         "what was the number I gave you"):
            self.assertEqual(classify_intent(question)[0], "chat", question)

        self.assertEqual(classify_intent("what is a monad")[0], "search")

    def test_plain_chat_does_not_search(self):
        # Only explicit intents are seeded; ordinary chat stays model-driven.
        seen: list[str] = []

        async def fake_tool(name, args, context):
            seen.append(name)
            return "{}"

        with patch("chat.llm.stream", text_stream("Hi there.")), \
             patch("chat.tools.execute_tool", fake_tool):
            self._run("say hello", Recorder())

        self.assertEqual(seen, [])

    # ── Memory toggle ──

    def test_memory_off_sends_no_history(self):
        ChatMessage.objects.create(
            session=self.session, role="user", content="my name is Ada"
        )
        ChatMessage.objects.create(
            session=self.session, role="assistant", content="Hello Ada"
        )
        self.session.memory_enabled = False
        self.session.save(update_fields=["memory_enabled"])

        seen: list[list[dict]] = []

        async def stream(**kwargs):
            seen.append(list(kwargs.get("history") or []))
            yield {"type": "content", "content": "ok"}

        recorder = Recorder()
        with patch("chat.llm.stream", stream):
            self._run("what is my name", recorder)

        self.assertEqual(seen[0], [])
        self.assertTrue(recorder.of(Event.STATUS))
        self.assertTrue(
            any(p.get("phase") == "memory_off" for p in recorder.of(Event.STATUS))
        )
        # Retention is not recall: the turns must still be in the database.
        self.assertEqual(
            ChatMessage.objects.filter(session=self.session, content="my name is Ada").count(),
            1,
        )

    def test_memory_on_sends_prior_turns(self):
        ChatMessage.objects.create(
            session=self.session, role="user", content="my name is Ada"
        )
        seen: list[list[dict]] = []

        async def stream(**kwargs):
            seen.append(list(kwargs.get("history") or []))
            yield {"type": "content", "content": "Ada"}

        with patch("chat.llm.stream", stream):
            self._run("what is my name", Recorder())

        self.assertIn("my name is Ada", json.dumps(seen[0]))

    # ── Failure handling ──

    def test_provider_failure_becomes_a_readable_message(self):
        from chat.llm import LLMUnavailable

        async def failing(**_kwargs):
            raise LLMUnavailable("No verified credential for 'openai'.")
            yield  # pragma: no cover — makes this an async generator

        with patch("chat.llm.stream", failing):
            outcome = self._run("hello", Recorder())

        # Never the raw error string, and never an empty bubble.
        self.assertTrue(outcome.assistant_message.content)
        self.assertNotIn("LLMUnavailable", outcome.assistant_message.content)
        self.assertIn("Settings", outcome.assistant_message.content)

    def test_empty_request_is_rejected(self):
        with self.assertRaises(TurnError):
            TurnRequest.parse({"content": "   "})


def fake_result_text() -> str:
    return json.dumps({"type": "search_results", "text": "Team A won.",
                       "sources": [{"url": "http://a", "title": "A"}]})


class ContextSummaryTests(TestCase):
    def test_prefers_an_explicit_conclusion(self):
        answer = (
            "Lots of detail. " * 50
            + "\n## Conclusion\nUse Postgres here because it scales better than "
              "the alternatives for this write-heavy workload."
        )
        self.assertIn("Postgres", context_summary(answer))

    def test_a_one_line_conclusion_falls_back_to_the_opening(self):
        # Under ~10 words a "conclusion" is usually a heading fragment rather
        # than a summary, and the opening carries more of the answer.
        answer = "Opening statement here. " * 50 + "\n## Conclusion\nUse Postgres."
        self.assertIn("Opening statement", context_summary(answer))

    def test_falls_back_to_the_opening(self):
        self.assertTrue(context_summary("Plain answer with no conclusion. " * 60))

    def test_markdown_is_stripped(self):
        summary = context_summary("**bold** `code` | table |\n" * 60)
        for marker in ("**", "`", "|"):
            self.assertNotIn(marker, summary)

    def test_length_is_bounded(self):
        self.assertLessEqual(len(context_summary("word " * 500).split()), 132)
