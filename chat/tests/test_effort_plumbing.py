"""
The effort knob as a *preference*, not a capability.

Everything about which rungs exist is `llm/tests/test_effort*.py`'s job. What is
tested here is the other half — that a level chosen in the UI survives the trip
to `TurnContext`, that a client which has never heard of the field behaves
exactly as it did before, and that the ancillary calls a turn makes on the
user's behalf do not quietly inherit the expensive setting.
"""
from __future__ import annotations

from django.contrib.auth import get_user_model
from django.test import TestCase

from chat.models import ChatSession
from chat.turn.pipeline import TurnRequest


class ParseTests(TestCase):
    """`llm_effort` has three meanings and they are not interchangeable."""

    def _parse(self, **payload):
        return TurnRequest.parse({"content": "hi", **payload})

    def test_an_absent_field_means_the_client_said_nothing(self):
        # None, not "" — a client that predates this field must leave whatever
        # the session already stored alone.
        self.assertIsNone(self._parse().effort)

    def test_a_level_is_read_and_normalised(self):
        self.assertEqual(self._parse(llm_effort=" High ").effort, "high")

    def test_an_empty_string_is_an_explicit_request_for_the_model_default(self):
        # Distinct from absent: this one has to be able to *clear* a level the
        # session already stored, which is the only way back off the knob.
        self.assertEqual(self._parse(llm_effort="").effort, "")

    def test_an_unrecognised_level_reads_as_saying_nothing(self):
        # Failing the whole turn over a preference typo would be the wrong
        # trade; the turn runs at whatever was already chosen.
        self.assertIsNone(self._parse(llm_effort="turbo").effort)


class SessionSyncTests(TestCase):
    """A chosen level is remembered the same way a chosen model is."""

    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="e", email="e@example.com", password="pw",
        )
        self.session = ChatSession.objects.create(
            user=self.user, llm_provider="nvidia", llm_model="m", llm_effort="",
        )

    def _sync(self, request, provider="nvidia", model="m", effort=""):
        from asgiref.sync import async_to_sync
        from chat.turn.pipeline import _sync_model_choice

        async_to_sync(_sync_model_choice)(
            self.session, request, provider, model, effort,
        )
        self.session.refresh_from_db()

    def test_a_new_session_starts_at_the_model_default(self):
        self.assertEqual(self.session.llm_effort, "")

    def test_choosing_only_an_effort_still_counts_as_an_override(self):
        # The bug this guards: requiring a model alongside it would mean a user
        # who changes nothing but how hard the model thinks has that choice
        # discarded on the next reload.
        self._sync(TurnRequest(content="hi", effort="high"), effort="high")
        self.assertEqual(self.session.llm_effort, "high")

    def test_a_client_that_says_nothing_changes_nothing(self):
        self.session.llm_effort = "medium"
        self.session.save(update_fields=["llm_effort"])
        self._sync(TurnRequest(content="hi"), effort="medium")
        self.assertEqual(self.session.llm_effort, "medium")

    def test_an_explicit_blank_clears_a_stored_level(self):
        self.session.llm_effort = "high"
        self.session.save(update_fields=["llm_effort"])
        self._sync(TurnRequest(content="hi", effort=""), effort="")
        self.assertEqual(self.session.llm_effort, "")


class TurnContextTests(TestCase):
    def test_the_default_turn_asks_for_no_effort(self):
        from chat.turn.agent import TurnContext

        turn = TurnContext(
            provider="nvidia", model="m", system_message="", user_id=1,
            session_id="s", intent="chat", user_text="hi",
        )
        # Chat behaves exactly as before until someone chooses.
        self.assertIsNone(turn.effort)

    def test_the_level_is_carried_unclamped(self):
        """`TurnContext` deliberately does not validate against the model.

        Which rungs a model offers is registry data; snapping here as well
        would be a second copy of that rule, and the copy that got forgotten
        would be the one that sent a bad level. `llm.access` owns it.
        """
        from chat.turn.agent import TurnContext

        turn = TurnContext(
            provider="nvidia", model="m", system_message="", user_id=1,
            session_id="s", intent="chat", user_text="hi", effort="minimal",
        )
        self.assertEqual(turn.effort, "minimal")


class IterationEffortTests(TestCase):
    """The tool loop thinks hardest where thinking shapes the most.

    The first pass plans the turn and the last permitted pass synthesises the
    answer, so both run at the chosen level; the dispatch passes in between
    run one rung down. Pinned here rather than as timing because the saving
    is in reasoning tokens the provider bills, not in wall clock we can
    assert on.
    """

    def _effort(self, base, iteration, at_limit=False):
        from chat.turn.agent import iteration_effort

        return iteration_effort(base, iteration, at_limit=at_limit)

    def test_the_first_pass_plans_at_full_effort(self):
        self.assertEqual(self._effort("high", 0), "high")

    def test_middle_passes_step_down_exactly_one_rung(self):
        self.assertEqual(self._effort("high", 1), "medium")
        self.assertEqual(self._effort("medium", 3), "low")
        self.assertEqual(self._effort("low", 2), "minimal")

    def test_the_final_pass_answers_at_full_effort(self):
        # Tools are withheld on this pass, so it is pure synthesis — the one
        # pass besides the first where the level is the answer's quality.
        self.assertEqual(self._effort("high", 7, at_limit=True), "high")

    def test_no_level_means_no_level_on_every_pass(self):
        self.assertIsNone(self._effort(None, 0))
        self.assertIsNone(self._effort(None, 4))
        # `""` is the explicit request for the model default, not an absent
        # level — it passes through exactly as `None` does.
        self.assertEqual(self._effort("", 2), "")

    def test_an_unknown_name_passes_through_untouched(self):
        # The snap in `llm.access` is what keeps a bad level off the wire;
        # guessing here would be a second copy of that rule.
        self.assertEqual(self._effort("turbo", 2), "turbo")

    def test_the_floor_holds_at_none(self):
        self.assertEqual(self._effort("none", 5), "none")
        self.assertEqual(self._effort("minimal", 5), "none")
