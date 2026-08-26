"""
Tests for the vision witness (`chat/vision/`, docs/VISION_AGENT.md).

The cases that matter are the ones where the feature must *not* fire: no witness
means today's behaviour unchanged, a failed look means a sentence rather than an
exception, and another user's attachment id means a refusal. A witness that works
on the happy path and leaks on the others is worse than no witness.
"""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

from asgiref.sync import async_to_sync
from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase

from chat.turn import agent as chat_agent
from chat.turn import history
from chat.models import ChatAttachment, ChatSession, VisionExchange
from chat.tools import execute_tool, get_available_tools
from chat.vision import agent as witness
from chat.vision import nim, resolve
from chat.vision.prompts import wants_verbatim

User = get_user_model()


# ─────────────────────────────────────────────────────────────────────────
# Disagreement — the whole uncertainty mechanism
# ─────────────────────────────────────────────────────────────────────────

class ReadingsDivergeTests(SimpleTestCase):
    def test_the_measured_failure_is_caught(self):
        # The real 2026-08-13 case: the VLM read 4.8 as 48, the parser read it
        # as 8.2. Neither hedged. Their disagreement is the only signal there is.
        self.assertTrue(
            witness.readings_diverge("The Q3 bar reads 48.", "Revenue (USD/MN) 8.2")
        )

    def test_agreement_is_not_flagged(self):
        self.assertFalse(
            witness.readings_diverge("The Q3 bar reads 4.8.", "Revenue (USD/MN) 4.8")
        )

    def test_commas_and_formatting_do_not_count_as_disagreement(self):
        self.assertFalse(witness.readings_diverge("Total 1,200", "Total 1200"))

    def test_no_numbers_means_no_signal(self):
        # A prose answer with nothing to cross-check must not be flagged; a
        # false alarm makes the main agent hedge something that was fine.
        self.assertFalse(witness.readings_diverge("A bar chart, blue.", "chart"))
        self.assertFalse(witness.readings_diverge("The bar reads 4.8", ""))

    def test_labels_are_not_read_as_numbers(self):
        # "Q3" must not contribute a 3: a witness naming the quarter it was
        # asked about would otherwise "disagree" with every correct parse.
        self.assertFalse(
            witness.readings_diverge("The Q3 bar reads 4.8.", "Revenue 4.8")
        )

    def test_extra_numbers_in_the_parse_are_not_disagreement(self):
        # The parser transcribes the whole page, so it always sees more than the
        # witness was asked about. Only a number the witness invented counts.
        self.assertFalse(witness.readings_diverge("Q3 is 4.8", "1.1 2.2 4.8 9.9"))


class WantsVerbatimTests(SimpleTestCase):
    def test_number_questions_trigger_the_cross_check(self):
        self.assertTrue(wants_verbatim("what are the four bar values?"))
        self.assertTrue(wants_verbatim("How much does it say at the top?"))

    def test_open_questions_do_not(self):
        self.assertFalse(wants_verbatim("Is this a photograph or a drawing?"))


# ─────────────────────────────────────────────────────────────────────────
# Per-turn budget
# ─────────────────────────────────────────────────────────────────────────

class TurnBudgetTests(SimpleTestCase):
    def setUp(self):
        witness._turn_budget.clear()

    def test_cap_applies_per_turn_and_attachment(self):
        for _ in range(witness.MAX_QUESTIONS_PER_TURN):
            self.assertTrue(witness._spend_budget("turn-1", "att-1"))
        self.assertFalse(witness._spend_budget("turn-1", "att-1"))
        # A different image in the same turn has its own budget, and so does the
        # same image in the next turn — the cap is against loops, not against use.
        self.assertTrue(witness._spend_budget("turn-1", "att-2"))
        self.assertTrue(witness._spend_budget("turn-2", "att-1"))

    def test_budget_table_stays_bounded(self):
        for i in range(witness._BUDGET_ENTRIES + 50):
            witness._spend_budget(f"turn-{i}", "att")
        self.assertLessEqual(len(witness._turn_budget), witness._BUDGET_ENTRIES)


# ─────────────────────────────────────────────────────────────────────────
# The parser adapter's two quirks
# ─────────────────────────────────────────────────────────────────────────

class ParseResponseTests(SimpleTestCase):
    def test_reads_the_transcript_out_of_a_tool_call(self):
        # nemotron-parse answers with content=null and the real payload in
        # tool_calls[0].function.arguments. Reading `content` gets nothing.
        body = {"choices": [{"message": {
            "content": None,
            "tool_calls": [{"function": {
                "name": "markdown_bbox",
                "arguments": json.dumps({"markdown_bbox": [
                    {"bbox": [0, 0, 1, 1], "text": "Revenue (USD/MN)", "type": "title"},
                    {"bbox": [1, 1, 2, 2], "text": "8.2", "type": "text"},
                ]}),
            }}],
        }}]}
        self.assertEqual(nim.extract_parse_text(body), "Revenue (USD/MN) | 8.2")

    def test_plain_content_still_wins_when_present(self):
        body = {"choices": [{"message": {"content": "4.8"}}]}
        self.assertEqual(nim.extract_parse_text(body), "4.8")

    def test_garbage_returns_none_rather_than_raising(self):
        self.assertIsNone(nim.extract_parse_text({}))
        self.assertIsNone(nim.extract_parse_text({"choices": [{"message": {
            "content": None, "tool_calls": [{"function": {"arguments": "not json"}}],
        }}]}))

    def test_mime_type_follows_the_filename(self):
        # The encoder used to label every attachment image/jpeg. The witness
        # depends on it, so a PNG has to say PNG.
        self.assertEqual(nim.mime_for("chart.png"), "image/png")
        self.assertEqual(nim.mime_for("photo.jpg"), "image/jpeg")
        self.assertEqual(nim.mime_for("no-extension"), "image/jpeg")


# ─────────────────────────────────────────────────────────────────────────
# Telling the main agent the witness exists
# ─────────────────────────────────────────────────────────────────────────

class BlockedAttachmentPointerTests(SimpleTestCase):
    def test_model_facing_text_carries_the_id(self):
        # An agent told a file exists but not its id can only apologise.
        text = history.describe_for_model([{
            "filename": "chart.png", "file_type": "image",
            "attachment_id": "4f2a-0001", "witness": True,
            "switch_model_helps": True, "reason": "x",
        }])
        self.assertIn("4f2a-0001", text)
        self.assertIn("ask_vision", text)

    def test_without_a_witness_it_stays_a_dead_end(self):
        text = history.describe_for_model([{
            "filename": "track.mp3", "file_type": "audio",
            "witness": False, "switch_model_helps": False,
            "reason": "Nothing can read it.",
        }])
        self.assertNotIn("ask_vision", text)
        self.assertIn("track.mp3", text)

    def test_empty_blocked_list_renders_nothing(self):
        self.assertEqual(history.describe_for_model([]), "")


class TextModelDescriptionTests(SimpleTestCase):
    class _Att:
        id = "4f2a-0002"
        filename = "chart.png"
        file_type = "image"
        extracted_text = ""

    def test_pointer_when_a_witness_exists(self):
        text = async_to_sync(chat_agent._describe_attachment_for_text_model)(
            self._Att(), witness=True
        )
        self.assertIn("ask_vision", text)
        self.assertIn("4f2a-0002", text)

    def test_apology_when_none_does(self):
        text = async_to_sync(chat_agent._describe_attachment_for_text_model)(
            self._Att(), witness=False
        )
        self.assertNotIn("ask_vision", text)
        self.assertIn("cannot see", text)


# ─────────────────────────────────────────────────────────────────────────
# Resolution and tool exposure
# ─────────────────────────────────────────────────────────────────────────

class ResolveWitnessTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("seer", "seer@example.com", "pw")

    def test_platform_key_is_enough(self):
        with patch("credentials.resolution.resolve_api_key_sync", return_value="k"):
            found = async_to_sync(resolve.resolve_witness)(self.user.id)
        self.assertIsNotNone(found)
        self.assertEqual(found.provider, "nvidia")
        self.assertEqual(found.model, resolve.DEFAULT_CHAIN[0])
        # The chain, not one model: NIM 404s for models its own catalog lists,
        # so a single configured model is one 404 away from having no eyes.
        self.assertGreater(len(found.models), 1)

    def test_no_key_means_no_witness(self):
        from credentials.resolution import CredentialUnavailable

        with patch("credentials.resolution.resolve_api_key_sync",
                   side_effect=CredentialUnavailable("none")):
            self.assertIsNone(async_to_sync(resolve.resolve_witness)(self.user.id))
            self.assertFalse(async_to_sync(resolve.witness_available)(self.user.id))

    def test_user_choice_leads_the_chain(self):
        from core.models import UserProfile

        UserProfile.objects.update_or_create(
            user=self.user,
            defaults={
                "vision_provider": "nvidia",
                "vision_model": "nvidia/llama-3.1-nemotron-nano-vl-8b-v1",
            },
        )

        with patch("credentials.resolution.resolve_api_key_sync", return_value="k"):
            found = async_to_sync(resolve.resolve_witness)(self.user.id)
        self.assertEqual(found.model, "nvidia/llama-3.1-nemotron-nano-vl-8b-v1")

    def test_anonymous_has_no_witness(self):
        self.assertFalse(async_to_sync(resolve.witness_available)(None))


class ToolExposureTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("looker", "looker@example.com", "pw")

    @staticmethod
    def _names(tools):
        return {t.get("function", {}).get("name") for t in tools}

    def test_offered_when_a_witness_exists(self):
        with patch("chat.vision.witness_available", AsyncMock(return_value=True)):
            tools = async_to_sync(get_available_tools)(self.user.id)
        self.assertIn("ask_vision", self._names(tools))

    def test_withheld_when_none_does(self):
        # An advertised tool that cannot run is worse than one never offered:
        # the model plans around it and then has to explain the failure.
        with patch("chat.vision.witness_available", AsyncMock(return_value=False)):
            tools = async_to_sync(get_available_tools)(self.user.id)
        self.assertNotIn("ask_vision", self._names(tools))

    def test_withheld_for_anonymous(self):
        tools = async_to_sync(get_available_tools)(None)
        self.assertNotIn("ask_vision", self._names(tools))

    def test_agent_runtime_does_not_inherit_it(self):
        # Phase 1 is chat only. The orchestrator toolbox is grant-driven, and
        # `ask_vision` is in no grant — this asserts it stays that way.
        from agents.agent.runtime import GRANT_TOOLS

        granted = {name for names in GRANT_TOOLS.values() for name in names}
        self.assertNotIn("ask_vision", granted)


# ─────────────────────────────────────────────────────────────────────────
# The tool itself
# ─────────────────────────────────────────────────────────────────────────

class AskVisionToolTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("owner", "owner@example.com", "pw")
        self.other = User.objects.create_user("thief", "thief@example.com", "pw")
        self.session = ChatSession.objects.create(user=self.user, title="s")
        self.attachment = ChatAttachment.objects.create(
            session=self.session, filename="chart.png", file_type="image",
            file="chat_attachments/2026/08/chart.png",
        )

    def _call(self, args, context):
        return async_to_sync(execute_tool)("ask_vision", args, context)

    def test_answer_comes_back_shaped_for_the_ui(self):
        with patch("chat.vision.ask", AsyncMock(return_value="Four bars: 4.2, 5.1, 4.8, 6.3.")):
            out = self._call(
                {"attachment_id": str(self.attachment.id), "question": "values?"},
                {"user_id": self.user.id, "session_id": str(self.session.id),
                 "turn_id": "t1"},
            )
        payload = json.loads(out)
        self.assertEqual(payload["type"], "vision_answer")
        self.assertEqual(payload["filename"], "chart.png")
        self.assertIn("4.8", payload["answer"])

    def test_another_users_attachment_is_refused(self):
        # Reachable directly through /api/chat/execute-tool/, so this check is
        # the only thing between a crafted id and a description of someone
        # else's upload.
        with patch("chat.vision.ask", AsyncMock(return_value="should not run")) as asked:
            out = self._call(
                {"attachment_id": str(self.attachment.id), "question": "values?"},
                {"user_id": self.other.id, "session_id": "x", "turn_id": "t1"},
            )
        self.assertIn("Access denied", out)
        asked.assert_not_awaited()

    def test_missing_arguments_are_reported_not_raised(self):
        self.assertIn("Missing attachment_id", self._call(
            {"question": "what?"}, {"user_id": self.user.id}))
        self.assertIn("Missing question", self._call(
            {"attachment_id": str(self.attachment.id)}, {"user_id": self.user.id}))

    def test_malformed_id_is_reported_not_raised(self):
        out = self._call({"attachment_id": "not-a-uuid", "question": "q"},
                         {"user_id": self.user.id})
        self.assertIn("Error", out)


class WitnessLoopTests(TestCase):
    def setUp(self):
        witness._turn_budget.clear()
        self.user = User.objects.create_user("watcher", "watcher@example.com", "pw")
        self.session = ChatSession.objects.create(user=self.user, title="s")
        self.attachment = ChatAttachment.objects.create(
            session=self.session, filename="chart.png", file_type="image",
            file="chat_attachments/2026/08/chart.png",
        )

    def _ask(self, question="what does it show?"):
        return async_to_sync(witness.ask)(
            self.attachment, question,
            session_id=str(self.session.id), user_id=self.user.id, turn_id="t1",
        )

    def test_no_witness_returns_a_sentence_not_an_exception(self):
        with patch("chat.vision.agent.resolve_witness", AsyncMock(return_value=None)):
            self.assertEqual(self._ask(), witness.VISION_UNAVAILABLE)

    def test_answer_is_persisted_for_the_next_question(self):
        found = resolve.Witness(provider="nvidia", models=("m1",))
        with patch("chat.vision.agent.resolve_witness", AsyncMock(return_value=found)), \
             patch("chat.vision.agent._call_witness",
                   AsyncMock(return_value=("A blue bar chart.", None))):
            answer = self._ask()

        self.assertEqual(answer, "A blue bar chart.")
        row = VisionExchange.objects.get(attachment=self.attachment)
        self.assertEqual(row.answer, "A blue bar chart.")
        self.assertEqual(row.model, "m1")
        self.assertFalse(row.disagreement)

    def test_unentitled_model_falls_through_to_the_next(self):
        found = resolve.Witness(provider="nvidia", models=("gone", "works"))
        calls = []

        async def fake(*, provider, model, question, transcript, attachment, user_id):
            calls.append(model)
            return (None, "unentitled") if model == "gone" else ("Seen.", None)

        with patch("chat.vision.agent.resolve_witness", AsyncMock(return_value=found)), \
             patch("chat.vision.agent._call_witness", side_effect=fake):
            self.assertEqual(self._ask(), "Seen.")
        self.assertEqual(calls, ["gone", "works"])

    def test_a_rejected_key_does_not_walk_the_chain(self):
        # Every model on that chain uses the same key, so trying the next one
        # just spends another round trip to fail identically.
        found = resolve.Witness(provider="nvidia", models=("a", "b"))
        calls = []

        async def fake(*, provider, model, question, transcript, attachment, user_id):
            calls.append(model)
            return None, "NVIDIA rejected the API key."

        with patch("chat.vision.agent.resolve_witness", AsyncMock(return_value=found)), \
             patch("chat.vision.agent._call_witness", side_effect=fake):
            self.assertEqual(self._ask(), witness.VISION_UNAVAILABLE)
        self.assertEqual(calls, ["a"])

    def test_disagreement_is_appended_and_recorded(self):
        found = resolve.Witness(provider="nvidia", models=("m1",))
        with patch("chat.vision.agent.resolve_witness", AsyncMock(return_value=found)), \
             patch("chat.vision.agent._call_witness",
                   AsyncMock(return_value=("The bar reads 48.", None))), \
             patch("chat.vision.agent._cross_check",
                   AsyncMock(return_value="Revenue (USD/MN) 8.2")):
            answer = self._ask("what value does the bar show?")

        self.assertIn("UNCERTAIN", answer)
        self.assertIn("8.2", answer)
        self.assertTrue(VisionExchange.objects.get(attachment=self.attachment).disagreement)

    def test_transcript_is_replayed_to_the_witness(self):
        VisionExchange.objects.create(
            session=self.session, attachment=self.attachment,
            question="what is it?", answer="A bar chart.", model="m1",
        )
        found = resolve.Witness(provider="nvidia", models=("m1",))
        seen = {}

        async def fake(*, provider, model, question, transcript, attachment, user_id):
            seen["transcript"] = transcript
            return "Four bars.", None

        with patch("chat.vision.agent.resolve_witness", AsyncMock(return_value=found)), \
             patch("chat.vision.agent._call_witness", side_effect=fake):
            self._ask("how many bars?")

        # Follow-ups are the reason this is an agent and not a function: the
        # second question must arrive with the first exchange in context.
        self.assertEqual(
            seen["transcript"],
            [{"role": "user", "content": "what is it?"},
             {"role": "assistant", "content": "A bar chart."}],
        )

    def test_budget_exhaustion_ends_the_interrogation(self):
        found = resolve.Witness(provider="nvidia", models=("m1",))
        with patch("chat.vision.agent.resolve_witness", AsyncMock(return_value=found)), \
             patch("chat.vision.agent._call_witness",
                   AsyncMock(return_value=("Still a chart.", None))):
            for _ in range(witness.MAX_QUESTIONS_PER_TURN):
                self._ask()
            capped = self._ask()

        self.assertIn("limit", capped)
        self.assertEqual(
            VisionExchange.objects.filter(attachment=self.attachment).count(),
            witness.MAX_QUESTIONS_PER_TURN,
        )

    def test_an_empty_question_is_refused_before_any_spend(self):
        with patch("chat.vision.agent.resolve_witness", AsyncMock()) as resolved:
            out = async_to_sync(witness.ask)(
                self.attachment, "   ",
                session_id=str(self.session.id), user_id=self.user.id, turn_id="t1",
            )
        self.assertIn("No question", out)
        resolved.assert_not_awaited()
