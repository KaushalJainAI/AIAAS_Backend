"""
A turn that cannot be paid for is reported, not performed.

The behaviour these lock in: when there is no credential, or the key is
rejected, or the balance is spent, the user is told straight away. What must
*not* happen is the client being shown "Starting up..." / "Processing your
message...", a spinner for the length of a real turn, and then an apology in
the assistant's voice — which reads as the model declining to answer rather
than as an account problem the user can go and fix.
"""
from __future__ import annotations

import json
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase

from llm import access as llm
from chat.turn.events import Event
from chat.models import ChatMessage, ChatSession
from chat.turn.pipeline import TurnError, TurnRequest, run_chat_turn

User = get_user_model()


def error_stream(message: str, status: int | None):
    """A provider stream that fails the way a real one does."""

    async def _stream(**_kwargs):
        yield {"type": "error", "message": message, "status": status}

    return _stream


class ClassifyProviderError(TestCase):
    """Which failures are the user's to fix, and which are ours."""

    def test_payment_required_is_a_quota_error(self):
        exc = llm.classify_provider_error(402, "Payment Required", "openrouter")
        self.assertIsInstance(exc, llm.LLMQuotaExhausted)
        self.assertIn("out of credit", str(exc))
        self.assertIn("OpenRouter", str(exc))

    def test_quota_wording_is_caught_without_a_status(self):
        """Streaming failures do not always carry a status code."""
        exc = llm.classify_provider_error(
            None, '{"error": {"code": "insufficient_quota"}}', "openai"
        )
        self.assertIsInstance(exc, llm.LLMQuotaExhausted)

    def test_rejected_key_is_an_access_error(self):
        exc = llm.classify_provider_error(401, "Invalid API key", "openai")
        self.assertIsInstance(exc, llm.LLMAccessDenied)
        self.assertIn("Settings", str(exc))

    def test_plain_rate_limit_is_not_an_account_error(self):
        """429 without billing wording clears on its own — not the user's problem."""
        self.assertIsNone(
            llm.classify_provider_error(429, "Rate limit exceeded, retry", "nvidia")
        )

    def test_server_error_is_not_an_account_error(self):
        self.assertIsNone(llm.classify_provider_error(503, "upstream down", "nvidia"))

    def test_a_quota_message_under_429_still_counts(self):
        """Some providers return 429 for a spent balance rather than 402."""
        self.assertIsInstance(
            llm.classify_provider_error(429, "insufficient credits", "openrouter"),
            llm.LLMQuotaExhausted,
        )

    def test_the_label_is_the_provider_name_not_the_picker_label(self):
        exc = llm.classify_provider_error(402, "", "openrouter")
        self.assertIn("OpenRouter", str(exc))
        self.assertNotIn("400+", str(exc))


class PreflightBeforeAnyWork(TestCase):
    """Nothing is streamed or stored until the call is known to be payable."""

    def setUp(self) -> None:
        self.user = User.objects.create_user(username="broke", password="pw")
        self.session = ChatSession.objects.create(
            user=self.user, title="T", llm_provider="openai", llm_model="gpt-4o",
        )
        self.events: list[tuple] = []

    async def _sink(self, event, payload) -> None:
        self.events.append((event, payload))

    def test_missing_credential_fails_before_the_first_status_event(self):
        """No credential for openai, and no platform key for it in tests."""
        from asgiref.sync import async_to_sync

        with self.assertRaises(TurnError) as caught:
            async_to_sync(run_chat_turn)(
                session=self.session,
                user=self.user,
                request=TurnRequest.parse({"content": "hello"}),
                sink=self._sink,
            )

        self.assertIn("credential", str(caught.exception).lower())
        self.assertIn("Settings", str(caught.exception))
        # The turn never announced itself…
        self.assertEqual(self.events, [])
        # …and nothing was written, so the message can simply be resent.
        self.assertFalse(ChatMessage.objects.filter(session=self.session).exists())

    def test_a_configured_provider_passes_preflight(self):
        """The platform NVIDIA key is enough — this must not become a gate."""
        from asgiref.sync import async_to_sync

        async_to_sync(llm.preflight)(
            provider="nvidia", model="nvidia/nemotron", user_id=self.user.id
        )

    def test_keyless_provider_needs_nothing(self):
        from asgiref.sync import async_to_sync

        async_to_sync(llm.preflight)(
            provider="ollama", model="phi4:latest", user_id=self.user.id
        )


class QuotaExhaustedMidTurn(TestCase):
    """Credit can run out after preflight has already passed."""

    def setUp(self) -> None:
        self.user = User.objects.create_user(username="midturn", password="pw")
        # nvidia resolves via the platform key, so preflight lets this through
        # and the failure has to be caught at the provider call.
        self.session = ChatSession.objects.create(
            user=self.user, title="T",
            llm_provider="nvidia", llm_model="nvidia/nemotron",
        )
        for target, replacement in (
            ("chat.turn.agent.suggest_follow_ups", self._none),
            ("chat.tools.get_available_tools", self._no_tools),
            ("chat.tools.execute_tool", self._inert),
        ):
            patcher = patch(target, replacement)
            patcher.start()
            self.addCleanup(patcher.stop)

    @staticmethod
    async def _none(*_a, **_k) -> list:
        return []

    @staticmethod
    async def _no_tools(*_a, **_k) -> list:
        return []

    @staticmethod
    async def _inert(*_a, **_k) -> str:
        return json.dumps({"type": "search_results", "text": "", "sources": []})

    def _run(self, stream):
        from asgiref.sync import async_to_sync

        events: list[tuple] = []

        async def sink(event, payload) -> None:
            events.append((event, payload))

        with patch("llm.access.stream", stream):
            try:
                async_to_sync(run_chat_turn)(
                    session=self.session,
                    user=self.user,
                    request=TurnRequest.parse({"content": "hello"}),
                    sink=sink,
                )
            except TurnError as exc:
                return events, exc
        return events, None

    def test_spent_balance_ends_the_turn_as_an_error(self):
        events, error = self._run(
            error_stream('{"error": "insufficient credits"}', 402)
        )
        self.assertIsNotNone(error, "an out-of-credit turn must not succeed")
        self.assertIn("out of credit", str(error))

        # The crucial part: it is not delivered as something the assistant said.
        content = [p for e, p in events if e == Event.CONTENT_CHUNK]
        self.assertEqual(content, [])
        self.assertFalse(
            ChatMessage.objects.filter(session=self.session, role="assistant").exists()
        )

    def test_a_transient_provider_error_is_still_handled_as_before(self):
        """Only account errors change shape; an outage must not start raising."""
        events, error = self._run(error_stream("upstream timeout", 503))
        self.assertIsNone(error)
        self.assertTrue(
            ChatMessage.objects.filter(session=self.session, role="assistant").exists()
        )


class RetiredModelTests(TestCase):
    """A model that reached end of life is a pick-another-model message.

    What the user saw before: the provider's RFC 7807 payload, verbatim, in the
    assistant's own message bubble behind a warning triangle —

        ⚠️ NVIDIA API error: {"type":"about:blank","title":"Gone","status":410,
        "detail":"The model 'deepseek-ai/deepseek-v4-flash' has reached its end
        of life on 2026-08-07T09:00:00Z and is no longer available."}

    Three things wrong with that: it reads as a crash, it is in the position
    where an answer goes, and it never says the one useful thing — pick a
    different model.
    """

    BODY = (
        '{"type":"about:blank","title":"Gone","status":410,"detail":"The model '
        "'deepseek-ai/deepseek-v4-flash' has reached its end of life on "
        '2026-08-07T09:00:00Z and is no longer available."}'
    )

    def test_a_410_is_a_model_problem_not_an_account_problem(self):
        exc = llm.classify_provider_error(
            410, self.BODY, "nvidia", "deepseek-ai/deepseek-v4-flash"
        )
        self.assertIsInstance(exc, llm.LLMModelUnavailable)
        # Not an account error: nothing is owed, so `agent_execute` must not
        # answer 402 and tell the user to top up something.
        self.assertNotIsInstance(exc, llm.LLMAccountError)
        # But still actionable, which is what makes it raise instead of render.
        self.assertIsInstance(exc, llm.LLMUserActionable)

    def test_the_message_names_the_model_the_provider_and_the_fix(self):
        exc = llm.classify_provider_error(
            410, self.BODY, "nvidia", "deepseek-ai/deepseek-v4-flash"
        )
        message = str(exc)
        self.assertIn("deepseek-ai/deepseek-v4-flash", message)
        self.assertIn("NVIDIA", message)
        self.assertIn("Pick a different model", message)
        # The provider's own sentence is kept — it carries the retirement date,
        # which is the part a user might want to check.
        self.assertIn("end of life", message)
        # And the protocol noise is gone.
        self.assertNotIn("about:blank", message)

    def test_end_of_life_wording_is_caught_without_a_status(self):
        """Some providers report it mid-stream, where no status rides along."""
        exc = llm.classify_provider_error(
            None, '{"error":{"message":"model_not_found"}}', "openai", "gpt-9"
        )
        self.assertIsInstance(exc, llm.LLMModelUnavailable)

    def test_an_unnamed_model_still_produces_a_usable_message(self):
        exc = llm.classify_provider_error(410, self.BODY, "nvidia")
        self.assertIn("That model is no longer available", str(exc))

    def test_a_live_model_is_not_swept_up(self):
        self.assertIsNone(
            llm.classify_provider_error(500, "Internal server error", "nvidia", "m")
        )


class HumanizeProviderBodyTests(TestCase):
    """Unclassified failures reach the user as prose, not as a payload."""

    def test_rfc7807_detail_is_pulled_out_and_the_prefix_kept(self):
        text = llm.humanize_provider_body(
            'NVIDIA API error: {"type":"about:blank","title":"Gone",'
            '"status":410,"detail":"The model is no longer available."}'
        )
        self.assertEqual(
            text, "NVIDIA API error: The model is no longer available."
        )

    def test_openai_nests_the_sentence_one_level_down(self):
        text = llm.humanize_provider_body(
            '{"error":{"message":"That model is overloaded.","type":"server_error"}}'
        )
        self.assertEqual(text, "That model is overloaded.")

    def test_plain_text_is_returned_untouched(self):
        self.assertEqual(
            llm.humanize_provider_body("Service Unavailable"), "Service Unavailable"
        )

    def test_unparseable_json_is_never_swallowed(self):
        """Better an ugly message than a blank one."""
        raw = 'NVIDIA API error: {not really json'
        self.assertEqual(llm.humanize_provider_body(raw), raw)

    def test_json_with_no_sentence_in_it_is_left_alone(self):
        raw = '{"status":500,"code":17}'
        self.assertEqual(llm.humanize_provider_body(raw), raw)
