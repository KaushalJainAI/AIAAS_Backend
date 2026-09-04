"""
Where the effort level is actually decided.

`llm.access` is the only place that knows both what the caller asked for and
what the model offers, which is why the snap happens there and not in
`TurnContext`, not in the serializer, and not in the handler. These tests pin
that: a caller may hand `_build_request` any level at all, and what comes out
in the handler config is always something this model serves — or nothing.
"""
from __future__ import annotations

from asgiref.sync import async_to_sync
from django.test import TestCase

from llm import access, effort
from llm.models import AIModel, AIProvider


class BuildRequestEffortTests(TestCase):
    def setUp(self):
        effort.clear_cache()
        self.provider, _ = AIProvider.objects.update_or_create(
            slug="openrouter", defaults={"name": "OpenRouter", "is_active": True},
        )
        AIModel.objects.update_or_create(
            value="t/reasoner",
            defaults={
                "provider": self.provider, "name": "Reasoner", "is_active": True,
                "is_free": True, "effort_levels": ["low", "medium", "high"],
                "default_effort": "",
            },
        )
        AIModel.objects.update_or_create(
            value="t/plain",
            defaults={
                "provider": self.provider, "name": "Plain", "is_active": True,
                "is_free": True, "effort_levels": [], "default_effort": "",
            },
        )

    # Ollama is the one keyless provider, so these exercise the funnel without
    # standing up a credential — the effort lookup is by model value and does
    # not care which provider the row hangs off.
    def _config(self, model, requested, *, primed=True):
        if primed:
            async_to_sync(effort.prime)(model)
        request = async_to_sync(access._build_request)(
            provider="ollama", model=model, prompt="hi", system_message="",
            user_id=1, temperature=0.2, max_tokens=64, effort=requested,
            tools=None, history=None, attachments=None,
        )
        return request.config

    def test_a_supported_level_passes_through(self):
        self.assertEqual(self._config("t/reasoner", "high")["effort"], "high")

    def test_a_level_the_model_lacks_is_snapped_not_refused(self):
        # The case this exists for: a user picks `minimal` on a GPT-5 tier, then
        # switches to a model whose cheapest rung is `low`. Failing the turn
        # over a preference would be the wrong trade.
        self.assertEqual(self._config("t/reasoner", "minimal")["effort"], "low")

    def test_a_model_with_no_effort_control_gets_no_level(self):
        # Not "gets the default level" — None, so the handler sends no field and
        # the provider is never asked something it would 400 on.
        self.assertIsNone(self._config("t/plain", "high")["effort"])

    def test_saying_nothing_stays_nothing(self):
        self.assertIsNone(self._config("t/reasoner", None)["effort"])

    def test_the_rows_default_applies_when_the_caller_is_silent(self):
        AIModel.objects.filter(value="t/reasoner").update(default_effort="medium")
        effort.clear_cache()
        self.assertEqual(self._config("t/reasoner", None)["effort"], "medium")

    def test_a_cold_cache_degrades_to_no_effort_rather_than_querying(self):
        """The hot path must not grow an `await` for this.

        `_build_request` runs on every model call including a streamed guest
        turn; a suspension point added there is enough to reorder when later
        work observes process state. So an unprimed model behaves exactly as it
        did before the knob existed: no field, no query, no failure.
        """
        effort.clear_cache()
        with self.assertNumQueries(0):
            config = self._config("t/reasoner", "high", primed=False)
        self.assertIsNone(config["effort"])

    def test_preflight_primes_what_the_hot_path_reads(self):
        async_to_sync(access.preflight)(
            provider="ollama", model="t/reasoner", user_id=1,
        )
        self.assertEqual(
            effort.cached_support("t/reasoner"), (("low", "medium", "high"), ""),
        )
