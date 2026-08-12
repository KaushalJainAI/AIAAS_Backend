"""
Tests for the shared LLM chat-completion plumbing.

These pin the behaviour that used to be copy-pasted into every provider node,
where it drifted. The `<think>`-splitter tests in particular encode the bug that
motivated the extraction: OpenAI's copy had been mangled into four consecutive
`text = delta["content"]` reassignments, so a chunk carrying an opening tag
emitted its leading text three times. `test_open_tag_emits_prefix_once` fails
against that version.

No test here makes a network call; SSE streams are built from literal lines.
"""
from __future__ import annotations

import json

from asgiref.sync import async_to_sync
from django.test import SimpleTestCase

from nodes.handlers.llm_base import (
    ChatChunkParser,
    iter_sse_chunks,
    split_think_tags,
)


class _FakeStream:
    """Minimal stand-in for httpx.Response.aiter_lines()."""

    def __init__(self, lines):
        self._lines = lines

    async def aiter_lines(self):
        for line in self._lines:
            yield line


def _collect(response, **kwargs):
    async def run():
        return [c async for c in iter_sse_chunks(response, **kwargs)]

    return async_to_sync(run)()


def _sse(*payloads):
    return _FakeStream([f"data: {json.dumps(p)}" for p in payloads] + ["data: [DONE]"])


def _delta(**delta):
    return {"choices": [{"delta": delta}]}


class SplitThinkTagsTests(SimpleTestCase):
    def test_plain_text_is_content(self):
        events, in_thinking = split_think_tags("hello", False)
        self.assertEqual(events, [("content", "hello")])
        self.assertFalse(in_thinking)

    def test_open_tag_emits_prefix_once(self):
        """The regression that motivated this module: prefix was emitted 3x."""
        events, in_thinking = split_think_tags("answer<think>because", False)
        self.assertEqual(
            events, [("content", "answer"), ("thinking", "because")]
        )
        self.assertTrue(in_thinking)

    def test_state_carries_across_chunks(self):
        events, state = split_think_tags("a<think>reason", False)
        self.assertTrue(state)
        events2, state2 = split_think_tags("ing more", state)
        self.assertEqual(events2, [("thinking", "ing more")])
        self.assertTrue(state2)
        events3, state3 = split_think_tags("done</think>final", state2)
        self.assertEqual(
            events3, [("thinking", "done"), ("content", "final")]
        )
        self.assertFalse(state3)

    def test_complete_block_in_one_chunk(self):
        events, state = split_think_tags("a<think>b</think>c", False)
        self.assertEqual(
            events,
            [("content", "a"), ("thinking", "b"), ("content", "c")],
        )
        self.assertFalse(state)

    def test_multiple_blocks_in_one_chunk(self):
        """The old splitter handled at most one tag pair per chunk."""
        events, state = split_think_tags("a<think>b</think>c<think>d</think>e", False)
        self.assertEqual(
            events,
            [
                ("content", "a"),
                ("thinking", "b"),
                ("content", "c"),
                ("thinking", "d"),
                ("content", "e"),
            ],
        )
        self.assertFalse(state)

    def test_empty_segments_are_not_emitted(self):
        events, _ = split_think_tags("<think>x</think>", False)
        self.assertEqual(events, [("thinking", "x")])


class IterSseChunksTests(SimpleTestCase):
    def test_parses_data_frames_and_stops_at_done(self):
        stream = _FakeStream(
            ['data: {"a": 1}', "data: [DONE]", 'data: {"never": true}']
        )
        self.assertEqual(_collect(stream), [{"a": 1}])

    def test_skips_blank_and_non_data_lines(self):
        stream = _FakeStream(["", ": keep-alive", 'data: {"a": 1}'])
        self.assertEqual(_collect(stream), [{"a": 1}])

    def test_unparseable_chunk_is_skipped_not_fatal(self):
        stream = _FakeStream(["data: {oops", 'data: {"a": 1}'])
        self.assertEqual(_collect(stream), [{"a": 1}])

    def test_bare_json_rejected_unless_opted_in(self):
        stream = _FakeStream(['{"a": 1}'])
        self.assertEqual(_collect(stream), [])
        self.assertEqual(
            _collect(_FakeStream(['{"a": 1}']), allow_bare_json=True), [{"a": 1}]
        )


class ChatChunkParserTests(SimpleTestCase):
    def _events(self, parser, *chunks):
        out = []
        for chunk in chunks:
            out.extend(parser.feed(chunk))
        return out

    def test_content_delta(self):
        self.assertEqual(
            self._events(ChatChunkParser(), _delta(content="hi")),
            [{"type": "content", "content": "hi"}],
        )

    def test_reasoning_keys_map_to_thinking(self):
        for key in ("reasoning_content", "reasoning"):
            with self.subTest(key=key):
                self.assertEqual(
                    self._events(ChatChunkParser(), _delta(**{key: "why"})),
                    [{"type": "thinking", "content": "why"}],
                )

    def test_thinking_suppressed_when_disabled(self):
        parser = ChatChunkParser(emit_thinking=False)
        self.assertEqual(self._events(parser, _delta(reasoning_content="why")), [])
        self.assertEqual(
            self._events(parser, _delta(content="a<think>b</think>c")),
            [
                {"type": "content", "content": "a"},
                {"type": "content", "content": "c"},
            ],
        )

    def test_tool_calls_can_be_disabled(self):
        call = [{"index": 0, "function": {"name": "f", "arguments": "{}"}}]
        self.assertEqual(
            self._events(ChatChunkParser(), _delta(tool_calls=call)),
            [{"type": "tool_calls", "tool_calls": call}],
        )
        self.assertEqual(
            self._events(ChatChunkParser(emit_tool_calls=False), _delta(tool_calls=call)),
            [],
        )

    def test_usage_frame_with_empty_choices(self):
        """OpenAI sends usage on a trailing frame carrying no choices."""
        usage = {"total_tokens": 12}
        self.assertEqual(
            self._events(ChatChunkParser(), {"choices": [], "usage": usage}),
            [{"type": "metadata", "usage": usage}],
        )

    def test_citations_are_forwarded(self):
        self.assertEqual(
            self._events(ChatChunkParser(), {"choices": [], "citations": ["u"]}),
            [{"type": "citations", "citations": ["u"]}],
        )

    def test_non_streaming_message_shape(self):
        """Some proxies deliver a whole message instead of a delta."""
        chunk = {"choices": [{"message": {"content": "whole"}}]}
        self.assertEqual(
            self._events(ChatChunkParser(), chunk),
            [{"type": "content", "content": "whole"}],
        )

    def test_thinking_state_spans_chunks(self):
        parser = ChatChunkParser()
        events = self._events(
            parser,
            _delta(content="a<think>b"),
            _delta(content="c"),
            _delta(content="d</think>e"),
        )
        self.assertEqual(
            events,
            [
                {"type": "content", "content": "a"},
                {"type": "thinking", "content": "b"},
                {"type": "thinking", "content": "c"},
                {"type": "thinking", "content": "d"},
                {"type": "content", "content": "e"},
            ],
        )

    def test_empty_delta_yields_nothing(self):
        self.assertEqual(self._events(ChatChunkParser(), _delta()), [])
