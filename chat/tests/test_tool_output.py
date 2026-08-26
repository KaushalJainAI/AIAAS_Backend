"""
The central bound on tool output, and the way back to what it cut.

The failure this replaces is silent. A tool with no budget of its own — every
MCP tool, since the response comes from a third-party server — returned whatever
it liked into the transcript, and `llm.clamp_input` then trimmed it from the
middle without saying so. A page that had been cut in half and a page that was
genuinely short looked exactly the same to the model.
"""
from __future__ import annotations

import json
from datetime import timedelta
from unittest.mock import patch

from asgiref.sync import async_to_sync
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from chat.models import ToolOutput
from chat.tools.tool_output import bound, read
from chat.tools import get_available_tools
from workflow_backend.thresholds import TOOL_OUTPUT_CHAR_LIMIT


class BoundingTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="bounder", email="b@example.test", password="x"
        )
        self.context = {
            "user_id": self.user.id, "session_id": "sess-1", "turn_id": "turn-1",
        }

    def test_output_within_the_limit_is_untouched(self):
        text = "a modest result"
        self.assertEqual(async_to_sync(bound)("web_search", text, self.context), text)
        self.assertEqual(ToolOutput.objects.count(), 0)

    def test_oversized_output_is_bounded_and_stored_whole(self):
        text = "x" * (TOOL_OUTPUT_CHAR_LIMIT + 5_000)
        result = async_to_sync(bound)("mcp_fetch", text, self.context)

        self.assertLess(len(result), len(text))
        row = ToolOutput.objects.get()
        self.assertEqual(row.total_chars, len(text))
        self.assertEqual(row.content, text)
        self.assertEqual(row.tool_name, "mcp_fetch")
        self.assertEqual(row.session_key, "sess-1")

    def test_the_model_is_told_what_was_cut_and_where_it_went(self):
        text = "y" * (TOOL_OUTPUT_CHAR_LIMIT + 5_000)
        result = async_to_sync(bound)("mcp_fetch", text, self.context)
        row = ToolOutput.objects.get()

        # Silence is the bug. The notice has to name the omission and the id.
        self.assertIn("characters omitted", result)
        self.assertIn(row.id, result)
        self.assertIn("read_tool_output", result)

    def test_both_ends_of_a_plain_result_survive(self):
        text = "HEAD" + ("m" * (TOOL_OUTPUT_CHAR_LIMIT + 5_000)) + "TAIL"
        result = async_to_sync(bound)("mcp_fetch", text, self.context)
        self.assertTrue(result.startswith("HEAD"))
        self.assertTrue(result.endswith("TAIL"))

    def test_structured_results_stay_parseable(self):
        # Head-and-tail on a JSON blob would corrupt it into unparseable prose
        # and take the sources with it, which is what the model cites from.
        payload = {
            "type": "search_results",
            "text": "z" * (TOOL_OUTPUT_CHAR_LIMIT + 5_000),
            "sources": ["https://example.test/a", "https://example.test/b"],
        }
        result = async_to_sync(bound)("web_search", json.dumps(payload), self.context)

        parsed = json.loads(result)
        self.assertEqual(parsed["sources"], payload["sources"])
        self.assertEqual(parsed["type"], "search_results")
        self.assertIn("characters omitted", parsed["text"])

    def test_a_failed_spill_still_returns_a_result(self):
        # A tool call that succeeded must stay succeeded. What changes is that
        # the notice stops promising an id it cannot honour.
        text = "q" * (TOOL_OUTPUT_CHAR_LIMIT + 5_000)

        async def _no_storage(*args, **kwargs):
            return None

        with patch("chat.tools.tool_output._spill", _no_storage):
            result = async_to_sync(bound)("mcp_fetch", text, self.context)

        self.assertIn("could not be stored", result)
        self.assertIn("unrecoverable", result)


class ReadBackTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="reader", email="r@example.test", password="x"
        )
        self.other = get_user_model().objects.create_user(
            username="stranger", email="s@example.test", password="x"
        )
        self.row = ToolOutput.objects.create(
            user=self.user, session_key="sess-1", turn_id="t1",
            tool_name="mcp_fetch", content="ABCDE" * 4_000, total_chars=20_000,
            expires_at=timezone.now() + timedelta(hours=1),
        )
        self.context = {"user_id": self.user.id, "session_id": "sess-1"}

    def test_reads_a_window_and_says_where_it_is(self):
        out = async_to_sync(read)(self.row.id, 0, self.context)
        self.assertTrue(out.startswith("ABCDE"))
        self.assertIn("of 20,000", out)
        self.assertIn("offset=", out)

    def test_offset_advances_through_the_text(self):
        first = async_to_sync(read)(self.row.id, 0, self.context)
        second = async_to_sync(read)(self.row.id, 12_000, self.context)
        self.assertNotEqual(first, second)
        self.assertIn("End of output", second)

    def test_offset_past_the_end_says_so(self):
        out = async_to_sync(read)(self.row.id, 99_999, self.context)
        self.assertIn("past the end", out)

    def test_another_user_cannot_read_it(self):
        out = async_to_sync(read)(
            self.row.id, 0, {"user_id": self.other.id, "session_id": "sess-1"}
        )
        self.assertIn("no stored output", out)

    def test_another_session_cannot_read_it(self):
        out = async_to_sync(read)(
            self.row.id, 0, {"user_id": self.user.id, "session_id": "sess-2"}
        )
        self.assertIn("no stored output", out)

    def test_expired_output_reports_expiry_rather_than_emptiness(self):
        self.row.expires_at = timezone.now() - timedelta(minutes=1)
        self.row.save(update_fields=["expires_at"])
        out = async_to_sync(read)(self.row.id, 0, self.context)
        self.assertIn("expired", out)


class AdvertisementTests(TestCase):
    """`read_tool_output` is withheld until there is something to read."""

    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="advertised", email="a@example.test", password="x"
        )

    def _names(self, **kwargs):
        tools = async_to_sync(get_available_tools)(self.user.id, **kwargs)
        return {t.get("function", {}).get("name") for t in tools}

    def _spill(self, session_key):
        ToolOutput.objects.create(
            user=self.user, session_key=session_key, tool_name="mcp_fetch",
            content="...", total_chars=3,
            expires_at=timezone.now() + timedelta(hours=1),
        )

    def test_not_offered_without_a_session(self):
        self.assertNotIn("read_tool_output", self._names())

    def test_not_offered_when_nothing_has_spilled(self):
        self.assertNotIn("read_tool_output", self._names(session_key="sess-1"))

    def test_offered_once_this_session_has_spilled(self):
        self._spill("sess-1")
        self.assertIn("read_tool_output", self._names(session_key="sess-1"))

    def test_another_session_spilling_does_not_offer_it_here(self):
        self._spill("sess-other")
        self.assertNotIn("read_tool_output", self._names(session_key="sess-1"))
