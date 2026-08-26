"""HTTP contract tests for the Imagine surface.

These assert the shape the frontend actually reads: `capabilities` must carry a
`defaults` map (the picker uses it to choose an initial model) and creating a
generation must reject a model the modality cannot run, rather than accepting
it and failing at the provider.
"""
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.cache import cache
from rest_framework.test import APITestCase

from imagine.services.capabilities import CACHE_KEY

CAPS = {
    "image": [
        {
            "id": "google/gemini-3.1-flash-image",
            "name": "Nano Banana 2",
            "provider": "google",
            "description": "",
            "resolutions": ["1K", "2K"],
            "aspect_ratios": ["1:1", "16:9"],
            "qualities": [],
            "max_batch": 1,
            "supports_seed": True,
            "supports_references": True,
        }
    ],
    "video": [
        {
            "id": "google/veo-3.1",
            "name": "Veo 3.1",
            "provider": "google",
            "description": "",
            "resolutions": ["720p"],
            "aspect_ratios": ["16:9"],
            "durations": [4, 8],
            "supports_audio": True,
            "supports_seed": False,
        }
    ],
    "audio": [
        {
            "id": "openai/gpt-4o-mini-tts",
            "name": "GPT-4o Mini TTS",
            "provider": "openai",
            "description": "",
            "voices": ["alloy"],
            "supports_speed": True,
        }
    ],
}


class ImagineApiTests(APITestCase):
    def setUp(self):
        cache.delete(CACHE_KEY)
        self.user = get_user_model().objects.create_user(
            username="imagine-user", email="i@example.com", password="pw",
        )
        self.client.force_authenticate(self.user)

    def tearDown(self):
        cache.delete(CACHE_KEY)

    # ── capabilities ─────────────────────────────────────────────────────────

    def test_capabilities_returns_models_and_defaults(self):
        with patch("imagine.views.OpenRouterService.for_user"), patch(
            "imagine.views.capabilities_for", return_value=CAPS
        ):
            response = self.client.get("/api/imagine/capabilities/")

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(len(body["image"]), 1)
        # The picker reads `defaults` to choose an initial model; without it
        # every modality would open on "Select a model".
        self.assertEqual(body["defaults"]["image"], "google/gemini-3.1-flash-image")
        self.assertEqual(body["defaults"]["video"], "google/veo-3.1")
        self.assertIn("recommended", body)

    def test_capabilities_reports_a_missing_credential_as_400(self):
        from imagine.services.openrouter import MissingOpenRouterCredentialError

        with patch(
            "imagine.views.OpenRouterService.for_user",
            side_effect=MissingOpenRouterCredentialError("no key"),
        ):
            response = self.client.get("/api/imagine/capabilities/")

        self.assertEqual(response.status_code, 400)
        body = response.json()
        self.assertIn("no key", body["detail"])
        # The frontend still renders the (empty) buckets, so they must exist.
        self.assertEqual(body["image"], [])

    def test_capabilities_requires_authentication(self):
        self.client.force_authenticate(None)
        self.assertEqual(self.client.get("/api/imagine/capabilities/").status_code, 401)

    # ── create ───────────────────────────────────────────────────────────────

    def _post(self, payload):
        # `perform_create` preflights the credential; give it one so the tests
        # that assert dispatch behaviour are not also asserting credential
        # handling. The preflight-only tests patch `for_user` themselves.
        with patch(
            "imagine.views.OpenRouterService.for_user", return_value=object()
        ), patch("imagine.serializers.capabilities_for", return_value=CAPS), patch(
            "imagine.views.run_generation"
        ) as run:
            response = self.client.post("/api/imagine/", payload, format="json")
        return response, run

    def test_create_rejects_a_model_from_the_wrong_modality(self):
        """A video model posted as an image used to reach OpenRouter and fail
        there, leaving a failed row and an opaque provider message."""
        response, run = self._post(
            {"type": "image", "prompt": "a fox", "model": "google/veo-3.1"}
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("model", response.json())
        run.assert_not_called()

    def test_create_accepts_a_valid_model_and_dispatches(self):
        response, run = self._post(
            {
                "type": "image",
                "prompt": "a fox",
                "model": "google/gemini-3.1-flash-image",
                "aspect_ratio": "16:9",
                "resolution": "2K",
            }
        )
        self.assertEqual(response.status_code, 201)
        run.assert_called_once()
        generation = run.call_args[0][0]
        self.assertEqual(generation.aspect_ratio, "16:9")
        self.assertEqual(generation.resolution, "2K")
        self.assertEqual(generation.user, self.user)

    def test_metadata_is_not_client_writable(self):
        response, run = self._post(
            {
                "type": "image",
                "prompt": "a fox",
                "model": "google/gemini-3.1-flash-image",
                "metadata": {"cost_usd": 999},
            }
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(run.call_args[0][0].metadata, {})

    def test_create_is_permitted_when_the_catalog_is_unavailable(self):
        """An empty catalog means OpenRouter is unreachable — that is not a
        reason to tell the user their model choice is wrong."""
        with patch(
            "imagine.views.OpenRouterService.for_user", return_value=object()
        ), patch(
            "imagine.serializers.capabilities_for",
            return_value={"image": [], "video": [], "audio": []},
        ), patch("imagine.views.run_generation"):
            response = self.client.post(
                "/api/imagine/",
                {"type": "image", "prompt": "a fox", "model": "anything/at-all"},
                format="json",
            )
        self.assertEqual(response.status_code, 201)

    def test_create_without_a_credential_is_rejected_before_dispatch(self):
        """A user with no OpenRouter key used to get a 201 with a row already
        marked failed — a configuration problem dressed as a generation."""
        from imagine.services.openrouter import MissingOpenRouterCredentialError

        with patch(
            "imagine.serializers.capabilities_for", return_value=CAPS
        ), patch(
            "imagine.views.OpenRouterService.for_user",
            side_effect=MissingOpenRouterCredentialError("add an OpenRouter key"),
        ), patch("imagine.views.run_generation") as run:
            response = self.client.post(
                "/api/imagine/",
                {
                    "type": "image",
                    "prompt": "a fox",
                    "model": "google/gemini-3.1-flash-image",
                },
                format="json",
            )

        self.assertEqual(response.status_code, 400)
        self.assertIn("add an OpenRouter key", response.json()["detail"])
        run.assert_not_called()

    # ── ownership ────────────────────────────────────────────────────────────

    def test_list_only_returns_the_callers_generations(self):
        from imagine.models import Generation

        other = get_user_model().objects.create_user(
            username="other", email="o@example.com", password="pw",
        )
        Generation.objects.create(user=other, type="image", prompt="theirs", model="m")
        mine = Generation.objects.create(user=self.user, type="image", prompt="mine", model="m")

        body = self.client.get("/api/imagine/").json()
        rows = body["results"] if isinstance(body, dict) else body
        self.assertEqual([r["id"] for r in rows], [mine.id])
