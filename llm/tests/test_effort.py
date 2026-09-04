"""
Effort levels: the vocabulary, the snap rule, and what reaches the wire.

The bug this whole feature has to avoid is one shape: a level that the model
does not serve arriving at the provider as a field. On OpenAI that is a hard
400 — the turn dies rather than degrading — so every test below is ultimately
about the same question, "does a field appear that should not".
"""
from __future__ import annotations

import pathlib

from django.test import TestCase

from llm import effort
from llm.handlers.llm_nodes import ollama_think
from llm.handlers.llm_providers import NvidiaNode, OpenAINode, OpenRouterNode
from llm.models import AIModel, AIProvider


class LadderTests(TestCase):
    def test_normalize_accepts_the_shapes_a_request_body_carries(self):
        self.assertEqual(effort.normalize(" High "), "high")
        self.assertEqual(effort.normalize("NONE"), "none")

    def test_normalize_rejects_everything_that_is_not_a_rung(self):
        for value in ("", "  ", "extreme", None, 3, ["high"]):
            self.assertIsNone(effort.normalize(value), value)

    def test_clean_levels_returns_ladder_order_not_written_order(self):
        self.assertEqual(
            effort.clean_levels(["high", "low", "none"]),
            ("none", "low", "high"),
        )

    def test_clean_levels_drops_junk_rather_than_raising(self):
        # An admin-edited row with one bad string costs that model its effort
        # control; it must not break the catalogue read for every other model.
        self.assertEqual(effort.clean_levels(["low", "banana"]), ("low",))
        self.assertEqual(effort.clean_levels("high"), ())
        self.assertEqual(effort.clean_levels(None), ())


class SnapTests(TestCase):
    """A level the model does not offer moves; it never fails the call."""

    def test_an_offered_level_is_returned_unchanged(self):
        self.assertEqual(effort.nearest("medium", effort.STANDARD), "medium")

    def test_an_unoffered_level_snaps_to_the_nearest_rung(self):
        # `minimal` is one below `low` and three below `high`.
        self.assertEqual(effort.nearest("minimal", effort.STANDARD), "low")
        self.assertEqual(effort.nearest("none", ("high",)), "high")

    def test_ties_break_downward(self):
        # `low` is one rung from both `minimal` and `medium`; the cheap side
        # wins, because nobody wants a surprise bill from a tie-break.
        self.assertEqual(effort.nearest("low", ("minimal", "medium")), "minimal")

    def test_a_model_offering_nothing_resolves_to_none(self):
        self.assertIsNone(effort.resolve("high", offered=()))

    def test_no_request_and_no_default_means_no_field(self):
        self.assertIsNone(effort.resolve(None, offered=effort.STANDARD))
        self.assertIsNone(effort.resolve("", offered=effort.STANDARD, default=""))

    def test_the_models_default_stands_in_when_the_caller_says_nothing(self):
        self.assertEqual(
            effort.resolve(None, offered=effort.STANDARD, default="high"), "high",
        )

    def test_the_caller_beats_the_default(self):
        self.assertEqual(
            effort.resolve("low", offered=effort.STANDARD, default="high"), "low",
        )

    def test_an_unrecognised_request_falls_back_to_the_default(self):
        self.assertEqual(
            effort.resolve("turbo", offered=effort.STANDARD, default="medium"),
            "medium",
        )


class RegistryLookupTests(TestCase):
    def setUp(self):
        effort.clear_cache()
        self.provider = AIProvider.objects.create(name="T", slug="t-effort")

    def test_support_is_read_from_the_row(self):
        AIModel.objects.create(
            provider=self.provider, name="R", value="t/reasoner",
            effort_levels=["high", "low"], default_effort="low",
        )
        levels, default = self._sync("t/reasoner")
        self.assertEqual(levels, ("low", "high"))
        self.assertEqual(default, "low")

    def test_a_default_outside_the_offered_set_is_snapped_not_sent(self):
        AIModel.objects.create(
            provider=self.provider, name="D", value="t/drifted",
            effort_levels=["low", "high"], default_effort="minimal",
        )
        _, default = self._sync("t/drifted")
        self.assertEqual(default, "low")

    def test_an_unknown_model_has_no_effort_control(self):
        self.assertEqual(self._sync("t/never-seen"), ((), ""))

    def test_the_hot_path_reads_cache_only(self):
        AIModel.objects.create(
            provider=self.provider, name="C", value="t/cached",
            effort_levels=["low", "medium"], default_effort="",
        )
        # Cold: the synchronous reader must not query, so it knows nothing.
        self.assertEqual(effort.cached_support("t/cached"), ((), ""))
        self._sync("t/cached")  # what `preflight` does
        self.assertEqual(effort.cached_support("t/cached"), (("low", "medium"), ""))

    def _sync(self, model):
        from asgiref.sync import async_to_sync

        return async_to_sync(effort.support_for)(model)


class WireEncodingTests(TestCase):
    """Each provider's own spelling of the same rung."""

    def _payload(self, node, level):
        return node.chat_payload(
            model="m", messages=[], config={"effort": level}, stream=False,
        )

    def test_openai_sends_a_bare_reasoning_effort_string(self):
        self.assertEqual(self._payload(OpenAINode(), "high")["reasoning_effort"], "high")

    def test_nvidia_copies_openais_field_name(self):
        self.assertEqual(self._payload(NvidiaNode(), "low")["reasoning_effort"], "low")

    def test_openrouter_wraps_the_level_in_a_reasoning_object(self):
        payload = self._payload(OpenRouterNode(), "medium")
        self.assertEqual(payload["reasoning"], {"effort": "medium"})
        self.assertNotIn("reasoning_effort", payload)

    def test_only_openrouter_can_actually_say_none(self):
        self.assertEqual(
            self._payload(OpenRouterNode(), "none")["reasoning"], {"enabled": False},
        )
        # OpenAI has no such rung, so the request degrades to its cheapest one
        # rather than dropping the field — dropping it would silently hand back
        # the model's *default* effort, the opposite of what was asked.
        self.assertEqual(
            self._payload(OpenAINode(), "none")["reasoning_effort"], "minimal",
        )

    def test_no_level_means_no_field_at_all(self):
        # The case that matters: a non-reasoning model must never see this key,
        # because OpenAI answers it with a 400 rather than ignoring it.
        for node in (OpenAINode(), NvidiaNode(), OpenRouterNode()):
            payload = self._payload(node, None)
            self.assertNotIn("reasoning_effort", payload, node)
            self.assertNotIn("reasoning", payload, node)

    def test_an_absent_key_is_the_same_as_none(self):
        payload = OpenAINode().chat_payload(
            model="m", messages=[], config={}, stream=False,
        )
        self.assertNotIn("reasoning_effort", payload)

    def test_ollama_spells_it_think_and_can_switch_it_off(self):
        self.assertEqual(ollama_think("high"), {"think": "high"})
        self.assertEqual(ollama_think("none"), {"think": False})
        # No `minimal` rung locally; it lands on the cheapest one that exists.
        self.assertEqual(ollama_think("minimal"), {"think": "low"})
        self.assertEqual(ollama_think(None), {})


class ShippedDefaultTests(TestCase):
    """The default model, the default level, and the link between them.

    A default effort is only real if the default *model* serves that rung —
    otherwise it is snapped away on the first message and the shipped default
    is not the shipped default. These two facts are set in different files
    (`chat/models.py` and `populate_models.py`), so nothing but a test connects
    them.
    """

    def test_the_chat_session_defaults_are_the_documented_trio(self):
        from chat.models import ChatSession

        fields = {f.name: f for f in ChatSession._meta.get_fields() if hasattr(f, "default")}
        self.assertEqual(fields["llm_provider"].default, "openrouter")
        self.assertEqual(fields["llm_model"].default, "openrouter/free")
        self.assertEqual(fields["llm_effort"].default, "medium")

    def test_the_profile_defaults_match_the_session_ones(self):
        # Two tables, one intent. They drifted before — the profile kept a
        # NVIDIA pair after chat had moved on — and a user whose profile is read
        # for a default gets a different model than their chat does.
        from chat.models import ChatSession
        from core.models import UserProfile

        for name in ("llm_provider", "llm_model", "llm_effort"):
            self.assertEqual(
                UserProfile._meta.get_field(name).default,
                ChatSession._meta.get_field(name).default,
                name,
            )

    def test_the_default_level_is_one_the_default_model_serves(self):
        """The check that makes the default effort more than a stored string.

        Read out of the seed script's source rather than out of the database,
        because `populate_models.py` is a seed script and not a migration: a
        fresh test database legitimately has no catalogue, and a version of this
        test that skipped on an empty table passed vacuously in exactly the
        environment that runs it. What is pinned is the declaration — if someone
        drops `effort=` from the default model's row, this fails.
        """
        from chat.models import ChatSession

        default_model = ChatSession._meta.get_field("llm_model").default
        default_level = ChatSession._meta.get_field("llm_effort").default

        source = (
            pathlib.Path(__file__).resolve().parents[2] / "populate_models.py"
        ).read_text(encoding="utf-8")
        row = next(
            (line for line in source.splitlines() if f'"{default_model}"' in line),
            None,
        )
        self.assertIsNotNone(row, f"{default_model} is not in the seed catalogue")
        self.assertIn(
            "effort=", row,
            f"the default model {default_model} declares no effort levels, so "
            f"the default level {default_level!r} would be snapped away on the "
            f"first message",
        )
        # And the constant it names has to contain the default rung.
        constant = row.split("effort=")[1].split(")")[0].split(",")[0].strip()
        self.assertIn(default_level, getattr(effort, constant.replace("EFFORT_", "")))

    def test_a_seeded_catalogue_agrees_with_the_declaration(self):
        """Belt and braces: when the row *is* present, it must match."""
        from chat.models import ChatSession

        row = AIModel.objects.filter(
            value=ChatSession._meta.get_field("llm_model").default
        ).first()
        if row is None:
            self.skipTest("catalogue not seeded in this database")
        self.assertIn(
            ChatSession._meta.get_field("llm_effort").default,
            effort.clean_levels(row.effort_levels),
        )
