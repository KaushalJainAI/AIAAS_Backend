"""Catalog normalization tests.

The bug these exist to prevent: `fetch_models` read `output_modalities` from
the top level of `/api/v1/models`, where the key does not exist — it lives
under `architecture`. Every bucket came back empty, so no model could be
selected anywhere in the UI and the agent asked a clarifying question it could
never satisfy. Nothing failed loudly; the catalog was simply always `[]`.

The fixtures below are trimmed from real OpenRouter payloads. The load-bearing
assertion is `test_buckets_are_never_empty` — a shape change that silently
empties a bucket fails here instead of in production.
"""
from unittest.mock import patch

from django.test import TestCase

from imagine.services import catalog
from imagine.services.openrouter import OpenRouterService

# ── fixtures (trimmed from live /images/models and /videos/models) ───────────

IMAGE_PAYLOAD = [
    {
        "id": "bytedance-seed/seedream-5-0-lite",
        "name": "ByteDance Seed: Seedream 5.0 Lite",
        "description": "Seedream 5.0 Lite is an image generation model.",
        "architecture": {"input_modalities": ["text", "image"], "output_modalities": ["image"]},
        "supported_parameters": {
            # Note: 1K absent — this model is 2K/4K only.
            "resolution": {"type": "enum", "values": ["2K", "4K"]},
            "aspect_ratio": {"type": "enum", "values": ["1:1", "16:9", "9:21"]},
            "n": {"type": "range", "min": 1, "max": 4},
            "input_references": {"type": "range", "min": 0, "max": 14},
            "seed": {"type": "boolean"},
        },
    },
    {
        "id": "google/gemini-3.1-flash-image",
        "name": "Google: Nano Banana 2 (Gemini 3.1 Flash Image)",
        "description": "",
        "architecture": {"input_modalities": ["text", "image"], "output_modalities": ["image"]},
        "supported_parameters": {
            "resolution": {"type": "enum", "values": ["1K", "2K", "4K"]},
            "aspect_ratio": {"type": "enum", "values": ["1:1", "16:9"]},
        },
    },
    {
        # Deliberately bare: no supported_parameters at all.
        "id": "recraft/recraft-v4",
        "name": "Recraft: Recraft V4",
        "architecture": {"input_modalities": ["text"], "output_modalities": ["image"]},
    },
]

VIDEO_PAYLOAD = [
    {
        "id": "bytedance/seedance-2.5",
        "name": "ByteDance: Seedance 2.5",
        "description": "Video generation model.",
        "supported_resolutions": ["480p", "720p"],
        "supported_aspect_ratios": ["16:9", "9:16", "21:9"],
        "supported_durations": [4, 5, 6, 10, 30],
        "generate_audio": True,
        "seed": True,
    },
    {
        "id": "google/veo-3.1",
        "name": "Google: Veo 3.1",
        "supported_resolutions": ["720p", "1080p"],
        "supported_aspect_ratios": ["16:9", "9:16"],
        "supported_durations": [4, 6, 8],
        "generate_audio": True,
        "seed": False,
    },
]


def _fake_catalog(path):
    return {"images/models": IMAGE_PAYLOAD, "videos/models": VIDEO_PAYLOAD}.get(path, [])


class CatalogNormalizationTests(TestCase):
    def _fetch(self):
        service = OpenRouterService(api_key="test-key")
        with patch.object(OpenRouterService, "_fetch_catalog", side_effect=_fake_catalog):
            return service.fetch_models()

    def test_buckets_are_never_empty(self):
        """The regression that shipped: every modality silently returned []."""
        caps = self._fetch()
        for kind in ("image", "video", "audio"):
            self.assertTrue(caps[kind], f"{kind} catalog is empty")

    def test_image_options_come_from_the_model_not_a_global_default(self):
        caps = self._fetch()
        lite = catalog.find_model(caps, "image", "bytedance-seed/seedream-5-0-lite")
        # The old code advertised 1K/2K/4K for every image model; this one
        # rejects 1K, and offering it produced a provider-side failure.
        self.assertEqual(lite["resolutions"], ["2K", "4K"])
        self.assertNotIn("1K", lite["resolutions"])
        self.assertEqual(lite["max_batch"], 4)
        self.assertTrue(lite["supports_seed"])
        self.assertTrue(lite["supports_references"])

    def test_model_without_parameters_still_gets_usable_options(self):
        """An empty option list renders as a dead control, so never emit one."""
        caps = self._fetch()
        recraft = catalog.find_model(caps, "image", "recraft/recraft-v4")
        self.assertTrue(recraft["resolutions"])
        self.assertTrue(recraft["aspect_ratios"])
        self.assertFalse(recraft["supports_seed"])

    def test_video_carries_durations_and_audio_support(self):
        caps = self._fetch()
        seedance = catalog.find_model(caps, "video", "bytedance/seedance-2.5")
        self.assertEqual(seedance["durations"], [4, 5, 6, 10, 30])
        self.assertTrue(seedance["supports_audio"])
        self.assertEqual(seedance["provider"], "bytedance")

    def test_audio_catalog_is_curated_and_carries_voices(self):
        """TTS models are absent from every OpenRouter discovery endpoint."""
        caps = self._fetch()
        ids = {m["id"] for m in caps["audio"]}
        self.assertIn("openai/gpt-4o-mini-tts", ids)
        gpt = catalog.find_model(caps, "audio", "openai/gpt-4o-mini-tts")
        self.assertIn("alloy", gpt["voices"])
        self.assertTrue(gpt["supports_speed"])

    def test_provider_is_populated(self):
        """The UI renders this field; it used to be absent and render blank."""
        caps = self._fetch()
        for kind in ("image", "video", "audio"):
            for model in caps[kind]:
                self.assertTrue(model["provider"], f"{model['id']} has no provider")

    def test_recommended_models_sort_first(self):
        caps = self._fetch()
        self.assertEqual(caps["image"][0]["id"], "google/gemini-3.1-flash-image")
        self.assertEqual(
            catalog.default_model_id("image", caps["image"]),
            "google/gemini-3.1-flash-image",
        )

    def test_default_falls_through_to_an_available_model(self):
        """A recommended id that is not in the live catalog must not be chosen."""
        pool = [{"id": "some/unlisted-model"}]
        self.assertEqual(catalog.default_model_id("image", pool), "some/unlisted-model")
        self.assertIsNone(catalog.default_model_id("image", []))

    def test_one_failing_endpoint_does_not_blank_the_others(self):
        service = OpenRouterService(api_key="test-key")
        with patch.object(
            OpenRouterService,
            "_fetch_catalog",
            side_effect=lambda p: [] if p == "images/models" else VIDEO_PAYLOAD,
        ):
            caps = service.fetch_models()
        self.assertEqual(caps["image"], [])
        self.assertTrue(caps["video"])
        self.assertTrue(caps["audio"])


class ImageRequestShapeTests(TestCase):
    """`POST /images` must carry only what the caller actually set."""

    def _capture_payload(self, config):
        service = OpenRouterService(api_key="test-key")
        with patch("imagine.services.openrouter.requests.post") as post:
            post.return_value.raise_for_status.return_value = None
            post.return_value.json.return_value = {
                "data": [{"b64_json": "QUJD", "media_type": "image/png"}],
                "usage": {"cost": 0.02},
            }
            result = service.generate_image("a fox", "black-forest-labs/flux.2-pro", config)
        return post.call_args.kwargs["json"], result

    def test_unset_options_are_omitted_entirely(self):
        """Sending `aspect_ratio: null` is what the old dispatcher did."""
        payload, _ = self._capture_payload({})
        self.assertEqual(payload["model"], "black-forest-labs/flux.2-pro")
        self.assertEqual(payload["prompt"], "a fox")
        for key in ("resolution", "aspect_ratio", "quality", "seed", "output_format"):
            self.assertNotIn(key, payload)

    def test_set_options_are_forwarded(self):
        payload, _ = self._capture_payload(
            {"resolution": "2K", "aspect_ratio": "16:9", "quality": "high", "seed": 7}
        )
        self.assertEqual(payload["resolution"], "2K")
        self.assertEqual(payload["aspect_ratio"], "16:9")
        self.assertEqual(payload["quality"], "high")
        self.assertEqual(payload["seed"], 7)

    def test_negative_prompt_is_folded_into_the_prompt(self):
        payload, _ = self._capture_payload({"negative_prompt": "text, watermark"})
        self.assertIn("a fox", payload["prompt"])
        self.assertIn("text, watermark", payload["prompt"])

    def test_base64_response_becomes_a_data_url(self):
        _, result = self._capture_payload({})
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["url"], "data:image/png;base64,QUJD")
        self.assertEqual(result["cost"], 0.02)

    def test_hosted_url_response_is_accepted(self):
        service = OpenRouterService(api_key="test-key")
        with patch("imagine.services.openrouter.requests.post") as post:
            post.return_value.raise_for_status.return_value = None
            post.return_value.json.return_value = {
                "data": [{"url": "https://cdn.example/img.png"}]
            }
            result = service.generate_image("a fox", "openai/gpt-image-2", {})
        self.assertEqual(result["url"], "https://cdn.example/img.png")


class DispatcherConfigTests(TestCase):
    """`_build_config` must omit unset fields rather than emit explicit nulls."""

    def _config(self, **kwargs):
        from imagine.models import Generation
        from imagine.services.dispatcher import _build_config
        defaults = {"type": "image", "prompt": "p", "model": "m"}
        return _build_config(Generation(**{**defaults, **kwargs}))

    def test_unset_fields_are_absent_not_none(self):
        """`config.get('aspect_ratio', '1:1')` returns None when the key exists
        with a null value — which is what reached OpenRouter."""
        config = self._config()
        for key in ("aspect_ratio", "resolution", "negative_prompt", "seed", "voice"):
            self.assertNotIn(key, config)
        self.assertEqual(config.get("aspect_ratio", "1:1"), "1:1")

    def test_image_resolution_maps_to_the_image_enum(self):
        self.assertEqual(self._config(resolution="2K")["resolution"], "2K")
        self.assertEqual(self._config(resolution="1024x1024")["resolution"], "1K")
        self.assertNotIn("resolution", self._config(resolution="banana"))

    def test_video_resolution_and_duration_use_the_video_enums(self):
        config = self._config(type="video", resolution="1080p", duration="10s")
        self.assertEqual(config["resolution"], "1080p")
        self.assertEqual(config["duration"], 10)

    def test_image_generations_carry_no_duration(self):
        self.assertNotIn("duration", self._config(duration="10"))

    def test_falsy_but_meaningful_values_survive(self):
        config = self._config(type="video", seed=0, generate_audio=False)
        self.assertEqual(config["seed"], 0)
        self.assertIs(config["generate_audio"], False)

    def test_decimal_durations_are_not_multiplied_by_ten(self):
        from imagine.services.dispatcher import _parse_seconds

        self.assertEqual(_parse_seconds("5s"), 5)
        self.assertEqual(_parse_seconds("10 seconds"), 10)
        self.assertEqual(_parse_seconds("1.5"), 1)
        self.assertEqual(_parse_seconds("banana", default=7), 7)
