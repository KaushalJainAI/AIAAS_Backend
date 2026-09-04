"""Intent classifier and HITL gate tests.

The classifier must never 500 the endpoint: a syntactically valid but
non-object model response is a model failure, not a request failure, and must
route through the same fallback as a dead LLM call. The cost gate must not
trust an under-reported number the request text itself could have talked the
model into.
"""
import sys
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.contrib.auth import get_user_model
from django.test import TestCase

from imagine.agent.hitl import needs_hitl
from imagine.agent.intent import classify


def _completion_returning(content):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content))]
    )


def _fake_litellm(completion):
    return SimpleNamespace(completion=completion)


def _patch_litellm(content):
    """`import litellm` happens inside `classify`, so the import itself must be
    redirected to the fake — there is no `imagine.agent.intent.litellm` module
    attribute to patch."""
    return patch.dict(sys.modules, {"litellm": _fake_litellm(_completion_returning(content))})


class ClassifierRobustnessTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="imagine-router", password="pw",
        )

    def _classify(self, message, content):
        with patch(
            "imagine.agent.intent.OpenRouterService.for_user",
            return_value=Mock(api_key="k"),
        ), patch(
            "imagine.agent.intent.capabilities_for",
            return_value={"image": [], "video": [], "audio": []},
        ), _patch_litellm(content):
            return classify(message, user=self.user)

    def test_non_object_json_routes_through_the_fallback(self):
        """`[]` is valid JSON; it is not an intent object. It used to crash on
        `.setdefault` outside the try and 500 the endpoint."""
        intent = self._classify("make a video", '["video"]')
        self.assertIsInstance(intent, dict)
        self.assertEqual(intent["type"], "video")  # heuristic fallback keyword

    def test_scalar_json_routes_through_the_fallback(self):
        intent = self._classify("a fox", '"image"')
        self.assertIsInstance(intent, dict)
        self.assertIn("missing_required", intent)

    def test_broken_json_routes_through_the_fallback(self):
        intent = self._classify("a fox", "{not json")
        self.assertIsInstance(intent, dict)
        self.assertIn("missing_required", intent)


class HITLGateTests(TestCase):
    def test_ambiguous_intent_is_gated(self):
        self.assertTrue(needs_hitl({"type": "image", "confidence": 0.5}, 0.10))
        self.assertTrue(needs_hitl({"type": "image", "clarifying_question": "which?"}, 0.10))
        self.assertTrue(needs_hitl({"type": "image", "missing_required": ["model"]}, 0.10))

    def test_video_is_always_gated(self):
        self.assertTrue(needs_hitl({"type": "video", "estimated_cost_usd": 0.001}, 0.10))

    def test_cost_gate_uses_the_modality_floor(self):
        # The router (or the request text behind it) cannot report below the
        # floor: at a threshold below the floor the gate fires regardless of
        # what the model claims the generation costs.
        self.assertTrue(needs_hitl(
            {"type": "image", "estimated_cost_usd": 0.001, "confidence": 0.9}, 0.01,
        ))
        # Above the floor and under the threshold, no gate — the normal case.
        self.assertFalse(needs_hitl(
            {"type": "image", "estimated_cost_usd": 0.001, "confidence": 0.9}, 0.10,
        ))

    def test_garbage_cost_is_treated_as_untrusted(self):
        self.assertTrue(needs_hitl({"type": "audio", "estimated_cost_usd": "garbage"}, 0.10))
        self.assertTrue(needs_hitl({"type": "image", "estimated_cost_usd": None}, 0.01))