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

from llm.handlers.llm_base import (
    ChatChunkParser,
    ReasoningSplitter,
    coerce_reasoning_text,
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


class ReasoningVocabularyTests(SimpleTestCase):
    """One endpoint serves several models, so all three tag styles must work."""

    def test_thinking_and_reasoning_tags_are_recognised(self):
        for open_, close in (("<thinking>", "</thinking>"),
                             ("<reasoning>", "</reasoning>")):
            with self.subTest(tag=open_):
                events, state = split_think_tags(f"a{open_}b{close}c", False)
                self.assertEqual(
                    events,
                    [("content", "a"), ("thinking", "b"), ("content", "c")],
                )
                self.assertFalse(state)

    def test_earliest_tag_wins_when_styles_are_mixed(self):
        events, _ = split_think_tags("a<think>b</think>c<thinking>d</thinking>e", False)
        self.assertEqual(
            events,
            [
                ("content", "a"), ("thinking", "b"), ("content", "c"),
                ("thinking", "d"), ("content", "e"),
            ],
        )


class OrphanCloseTagTests(SimpleTestCase):
    """
    R1-distill chat templates prefill `<think>` into the prompt, so the model's
    output starts inside reasoning and only ever emits the closing tag.
    """

    def test_close_without_open_is_reasoning_not_answer(self):
        events, state = split_think_tags("weighing it up</think>The answer.", False)
        self.assertEqual(
            events,
            [("thinking", "weighing it up"), ("content", "The answer.")],
        )
        self.assertFalse(state)

    def test_stray_tag_never_reaches_the_user(self):
        events, _ = split_think_tags("reasoning</think>answer", False)
        for _, text in events:
            self.assertNotIn("</think>", text)

    def test_close_after_real_content_is_treated_as_prose(self):
        """A model writing *about* tags must not have its answer reclassified."""
        splitter = ReasoningSplitter()
        self.assertEqual(splitter.feed("You close it with "), [("content", "You close it with ")])
        self.assertEqual(splitter.feed("</think> like so."), [("content", " like so.")])


class TornTagTests(SimpleTestCase):
    """A tag does not respect chunk boundaries."""

    def test_tag_split_across_chunks_is_not_shown_as_content(self):
        splitter = ReasoningSplitter()
        self.assertEqual(splitter.feed("answer<thi"), [("content", "answer")])
        self.assertEqual(splitter.feed("nk>reason"), [("thinking", "reason")])
        self.assertTrue(splitter.in_thinking)

    def test_held_text_is_released_when_it_is_not_a_tag(self):
        """A lone `<` is held one chunk, then released once it cannot be a tag."""
        splitter = ReasoningSplitter()
        self.assertEqual(splitter.feed("a <"), [("content", "a ")])
        self.assertEqual(splitter.feed(" b"), [("content", "< b")])

    def test_text_that_cannot_extend_a_tag_is_not_held(self):
        splitter = ReasoningSplitter()
        self.assertEqual(splitter.feed("a < b"), [("content", "a < b")])

    def test_flush_releases_a_dangling_partial_tag(self):
        splitter = ReasoningSplitter()
        self.assertEqual(splitter.feed("done<"), [("content", "done")])
        self.assertEqual(splitter.flush(), [("content", "<")])
        self.assertEqual(splitter.flush(), [])

    def test_nothing_is_lost_across_a_torn_stream(self):
        splitter = ReasoningSplitter()
        seen = ""
        for piece in ["he", "llo<th", "ink>why", " not</thi", "nk>bye"]:
            seen += "".join(t for _, t in splitter.feed(piece))
        seen += "".join(t for _, t in splitter.flush())
        self.assertEqual(seen, "hellowhy notbye")


class CoerceReasoningTextTests(SimpleTestCase):
    def test_plain_string_passes_through(self):
        self.assertEqual(coerce_reasoning_text("why"), "why")

    def test_openrouter_reasoning_details_list(self):
        self.assertEqual(
            coerce_reasoning_text([
                {"type": "reasoning.text", "text": "first "},
                {"type": "reasoning.summary", "summary": "second"},
            ]),
            "first second",
        )

    def test_nested_object_is_unwrapped(self):
        self.assertEqual(coerce_reasoning_text({"content": "why"}), "why")

    def test_unreadable_entry_yields_nothing(self):
        """`reasoning.encrypted` is an opaque blob, not the model's thoughts."""
        self.assertEqual(
            coerce_reasoning_text({"type": "reasoning.encrypted", "data": "aGk="}),
            "",
        )
        self.assertEqual(coerce_reasoning_text(None), "")


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

    def test_newer_reasoning_keys_map_to_thinking(self):
        """OpenRouter adds `reasoning_details`; Anthropic proxies use `thinking`."""
        for key in ("reasoning_details", "thinking"):
            with self.subTest(key=key):
                self.assertEqual(
                    self._events(ChatChunkParser(), _delta(**{key: "why"})),
                    [{"type": "thinking", "content": "why"}],
                )

    def test_structured_reasoning_is_flattened_to_text(self):
        """A dict reaching a `thinking += content` accumulator kills the stream."""
        chunk = _delta(reasoning_details=[{"type": "reasoning.text", "text": "why"}])
        self.assertEqual(
            self._events(ChatChunkParser(), chunk),
            [{"type": "thinking", "content": "why"}],
        )

    def test_reasoning_toggle_off(self):
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
