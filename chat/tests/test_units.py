"""
Unit tests for the chat agent's plumbing.

These replace the old suite, which exercised a 20-regex tool-call scraper and a
JSON-envelope parser that no longer exist. What is tested here is what the new
design actually depends on: stream folding, the weak-model fallback, and the
follow-up parse.
"""
from __future__ import annotations

from django.test import SimpleTestCase

from chat.turn.extraction import extract_text_tool_calls, split_text_tool_calls
from llm.access import StreamAccumulator, to_tool_calls
from chat.turn.agent import _parse_follow_ups


class StreamAccumulatorTests(SimpleTestCase):
    """Provider streams arrive fragmented; folding them must be exact."""

    def test_content_and_thinking_are_kept_apart(self):
        acc = StreamAccumulator()
        acc.add({"type": "thinking", "content": "hmm..."})
        acc.add({"type": "content", "content": "Hello"})
        acc.add({"type": "content", "content": " world"})

        result = acc.finish()
        self.assertEqual(result.content, "Hello world")
        self.assertEqual(result.thinking, "hmm...")

    def test_tool_call_deltas_are_reassembled_by_index(self):
        # Name arrives in one chunk, the argument JSON split across several.
        acc = StreamAccumulator()
        acc.add({"type": "tool_calls", "tool_calls": [
            {"index": 0, "id": "call_1", "function": {"name": "web_search"}},
        ]})
        acc.add({"type": "tool_calls", "tool_calls": [
            {"index": 0, "function": {"arguments": '{"que'}},
        ]})
        acc.add({"type": "tool_calls", "tool_calls": [
            {"index": 0, "function": {"arguments": 'ry": "rust"}'}},
        ]})

        calls = acc.finish().tool_calls
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].id, "call_1")
        self.assertEqual(calls[0].name, "web_search")
        self.assertEqual(calls[0].arguments, {"query": "rust"})

    def test_parallel_calls_keep_their_own_arguments(self):
        acc = StreamAccumulator()
        for index, (name, args) in enumerate(
            [("web_search", '{"query": "a"}'), ("image_search", '{"query": "b"}')]
        ):
            acc.add({"type": "tool_calls", "tool_calls": [
                {"index": index, "id": f"c{index}",
                 "function": {"name": name, "arguments": args}},
            ]})

        calls = acc.finish().tool_calls
        self.assertEqual([c.name for c in calls], ["web_search", "image_search"])
        self.assertEqual([c.arguments["query"] for c in calls], ["a", "b"])

    def test_has_tool_calls_is_true_before_the_stream_ends(self):
        # Drives the decision to stop streaming content live, so it must flip as
        # soon as a name appears rather than at finish().
        acc = StreamAccumulator()
        self.assertFalse(acc.has_tool_calls)
        acc.add({"type": "tool_calls", "tool_calls": [
            {"index": 0, "function": {"name": "web_search"}},
        ]})
        self.assertTrue(acc.has_tool_calls)

    def test_usage_is_summed_across_chunks(self):
        acc = StreamAccumulator()
        acc.add({"type": "metadata", "usage": {"total_tokens": 30}})
        acc.add({"type": "metadata",
                 "usage": {"prompt_tokens": 5, "completion_tokens": 7}})
        self.assertEqual(acc.finish().tokens, 42)

    def test_error_chunk_is_captured(self):
        acc = StreamAccumulator()
        acc.add({"type": "error", "message": "rate limited"})
        self.assertEqual(acc.error, "rate limited")


class ToolCallNormalisationTests(SimpleTestCase):
    def test_unparseable_arguments_become_an_empty_dict(self):
        # Better an empty argument set the tool can reject than a crash.
        calls = to_tool_calls([
            {"id": "1", "function": {"name": "web_search", "arguments": "not json"}},
        ])
        self.assertEqual(calls[0].arguments, {})

    def test_nameless_fragments_are_dropped(self):
        self.assertEqual(to_tool_calls([{"function": {"arguments": "{}"}}]), ())

    def test_missing_id_is_synthesised(self):
        calls = to_tool_calls([{"function": {"name": "get_current_time"}}])
        self.assertTrue(calls[0].id)


class TextToolCallFallbackTests(SimpleTestCase):
    """
    The fallback for small local models that write calls as text. Capable models
    emit native tool_calls and never reach this.
    """

    def test_delimited_call(self):
        calls = extract_text_tool_calls(
            '[TOOL_CALL]{"tool": "web_search", "args": {"query": "python"}}[/TOOL_CALL]'
        )
        self.assertEqual(calls[0].name, "web_search")
        self.assertEqual(calls[0].arguments, {"query": "python"})

    def test_bare_json_object(self):
        calls = extract_text_tool_calls(
            'Sure, let me look.\n{"name": "web_search", "arguments": {"query": "x"}}'
        )
        self.assertEqual(calls[0].name, "web_search")

    def test_inline_arguments_without_a_nested_key(self):
        calls = extract_text_tool_calls('{"tool": "web_search", "query": "inline"}')
        self.assertEqual(calls[0].arguments, {"query": "inline"})

    def test_raw_syntax_is_removed_from_the_message(self):
        # The user must never see the model's internal call syntax.
        text = 'Checking.\n[TOOL_CALL]{"tool": "web_search", "args": {"query": "q"}}[/TOOL_CALL]'
        calls, cleaned = split_text_tool_calls(text)

        self.assertEqual(len(calls), 1)
        self.assertEqual(cleaned, "Checking.")
        self.assertNotIn("TOOL_CALL", cleaned)

    def test_ordinary_prose_is_not_a_tool_call(self):
        self.assertEqual(extract_text_tool_calls("Here is the answer."), ())

    def test_json_that_is_not_a_call_is_left_alone(self):
        # A model explaining a JSON payload must not be read as calling a tool.
        self.assertEqual(
            extract_text_tool_calls('Config: {"timeout": 30, "retries": 2}'), ()
        )

    def test_empty_input(self):
        self.assertEqual(extract_text_tool_calls(""), ())
        self.assertEqual(split_text_tool_calls(""), ((), ""))


class FollowUpParsingTests(SimpleTestCase):
    def test_plain_object(self):
        self.assertEqual(
            _parse_follow_ups('{"follow_ups": ["A?", "B?"]}', 3), ["A?", "B?"]
        )

    def test_surrounding_prose_and_fences_are_tolerated(self):
        self.assertEqual(
            _parse_follow_ups('```json\n{"follow_ups": ["A?"]}\n```', 3), ["A?"]
        )

    def test_limit_is_applied(self):
        self.assertEqual(
            len(_parse_follow_ups('{"follow_ups": ["1", "2", "3", "4"]}', 3)), 3
        )

    def test_blank_entries_are_dropped(self):
        self.assertEqual(_parse_follow_ups('{"follow_ups": ["A?", "  "]}', 3), ["A?"])

    def test_junk_yields_no_suggestions(self):
        # Follow-ups are optional garnish; never worth surfacing a parse failure.
        for junk in ("", "sorry, I can't", '{"follow_ups": "not a list"}', "{broken"):
            self.assertEqual(_parse_follow_ups(junk, 3), [])
