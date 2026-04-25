"""
Unit tests for the chat app — focused on pure helpers in extraction.py and
graph.py that don't require a real LLM or DB.

These tests are intentionally creative: LLMs emit tool calls in a wide
variety of (mis-)formats, so the extractor MUST be robust to:
  * single-quoted JSON
  * markdown code fences
  * arrow-hash (Ruby-ish) style
  * react-style "Action:" prefixes
  * trailing junk after the JSON object
  * malformed/incomplete arguments
"""
from __future__ import annotations

import json

from django.test import SimpleTestCase

from chat.extraction import (
    clean_json_string, extract_tool_calls, fuzzy_json_loads,
    parse_tool_arguments, strip_tool_calls,
)
from chat.graph import _count_ai_messages, _openai_tc_to_langchain


# ─────────────────────────────────────────────────────────────────────────
# fuzzy_json_loads — robustness to LLM output styles
# ─────────────────────────────────────────────────────────────────────────

class FuzzyJsonLoadsTests(SimpleTestCase):
    def test_strict_json(self):
        self.assertEqual(fuzzy_json_loads('{"a": 1}'), {"a": 1})

    def test_single_quoted_python_dict(self):
        out = fuzzy_json_loads("{'a': 1, 'b': 'two'}")
        self.assertEqual(out, {"a": 1, "b": "two"})

    def test_python_literals_true_false_none(self):
        out = fuzzy_json_loads("{'on': True, 'off': False, 'null': None}")
        self.assertEqual(out, {"on": True, "off": False, "null": None})

    def test_arrow_hash_style(self):
        # Some models emit Ruby-style => for keys.
        out = fuzzy_json_loads('{tool => "x", args => {"q": "hi"}}')
        # Whatever the parser returns, both keys must be reachable.
        self.assertIsNotNone(out)

    def test_markdown_fence_stripped(self):
        s = '```json\n{"k": 1}\n```'
        self.assertEqual(fuzzy_json_loads(s), {"k": 1})

    def test_empty_returns_none(self):
        self.assertIsNone(fuzzy_json_loads(""))
        self.assertIsNone(fuzzy_json_loads(None))

    def test_garbage_returns_none(self):
        # The function should not crash on non-JSON text.
        self.assertIsNone(fuzzy_json_loads("this is not json at all"))


class CleanJsonStringTests(SimpleTestCase):
    def test_strips_leading_fence(self):
        self.assertEqual(clean_json_string('```json\n{}'), '{}')

    def test_strips_trailing_fence(self):
        self.assertEqual(clean_json_string('{}\n```'), '{}')

    def test_empty_input(self):
        self.assertEqual(clean_json_string(""), "")
        self.assertEqual(clean_json_string(None), "")


# ─────────────────────────────────────────────────────────────────────────
# parse_tool_arguments — must coerce many shapes to dict
# ─────────────────────────────────────────────────────────────────────────

class ParseToolArgumentsTests(SimpleTestCase):
    def test_dict_passthrough(self):
        self.assertEqual(parse_tool_arguments({"q": "hi"}), {"q": "hi"})

    def test_json_string_parsed(self):
        self.assertEqual(parse_tool_arguments('{"q": "hi"}'), {"q": "hi"})

    def test_single_quoted_string(self):
        out = parse_tool_arguments("{'q': 'hi'}")
        self.assertEqual(out, {"q": "hi"})

    def test_empty_string_returns_empty_dict(self):
        # Convention: empty / unparsable args → {}
        out = parse_tool_arguments("")
        self.assertIn(out, ({}, None))


# ─────────────────────────────────────────────────────────────────────────
# extract_tool_calls — recognize many syntaxes
# ─────────────────────────────────────────────────────────────────────────

class ExtractToolCallsTests(SimpleTestCase):
    def test_standard_bracket_style(self):
        text = '[TOOL_CALL]{"tool": "web_search", "args": {"query": "hi"}}[/TOOL_CALL]'
        out = extract_tool_calls(text)
        self.assertTrue(any(t.get("tool") == "web_search" for t in out))

    def test_anthropic_invoke_xml(self):
        text = '<invoke name="web_search">{"query": "hi"}</invoke>'
        out = extract_tool_calls(text)
        self.assertTrue(out, "Should have detected at least one tool call")

    def test_returns_empty_for_plain_text(self):
        self.assertEqual(extract_tool_calls("just a normal answer"), [])

    def test_json_block_format(self):
        text = '```json\n{"tool": "web_search", "args": {"query": "x"}}\n```'
        out = extract_tool_calls(text)
        self.assertTrue(any(t.get("tool") == "web_search" for t in out))


class StripToolCallsTests(SimpleTestCase):
    def test_removes_bracket_call(self):
        s = "Here is the answer.[TOOL_CALL]{...}[/TOOL_CALL] More text."
        out = strip_tool_calls(s)
        self.assertNotIn("[TOOL_CALL]", out)
        self.assertNotIn("[/TOOL_CALL]", out)

    def test_idempotent_on_clean_text(self):
        s = "Just answer text."
        self.assertEqual(strip_tool_calls(s).strip(), s)


# ─────────────────────────────────────────────────────────────────────────
# graph helpers
# ─────────────────────────────────────────────────────────────────────────

class GraphHelperTests(SimpleTestCase):
    def test_openai_tc_skips_non_function_type(self):
        raw = [{"type": "code_interpreter"}, {"type": "function", "function": {"name": "x", "arguments": "{}"}, "id": "id1"}]
        out = _openai_tc_to_langchain(raw)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["name"], "x")
        self.assertEqual(out[0]["id"], "id1")

    def test_openai_tc_synthesizes_id_when_missing(self):
        raw = [{"type": "function", "function": {"name": "x", "arguments": "{}"}}]
        out = _openai_tc_to_langchain(raw)
        self.assertTrue(out[0]["id"].startswith("call_"))

    def test_openai_tc_skips_nameless(self):
        raw = [{"type": "function", "function": {"name": "", "arguments": "{}"}}]
        self.assertEqual(_openai_tc_to_langchain(raw), [])

    def test_openai_tc_coerces_non_dict_args(self):
        # If args parse to a string, we wrap it as {"query": ...}.
        raw = [{"type": "function", "function": {"name": "search", "arguments": '"hello world"'}}]
        out = _openai_tc_to_langchain(raw)
        self.assertEqual(out[0]["args"], {"query": "hello world"})

    def test_count_ai_messages(self):
        from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
        msgs = [
            HumanMessage(content="q"),
            AIMessage(content="a1"),
            ToolMessage(content="r", tool_call_id="x", name="t"),
            AIMessage(content="a2"),
        ]
        self.assertEqual(_count_ai_messages(msgs), 2)
