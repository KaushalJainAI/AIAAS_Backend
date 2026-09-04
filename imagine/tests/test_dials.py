"""
Only the dials this model takes, and only the values it accepts.

Every case here was measured against the live API before it was written. The
two failure modes are asymmetric, and that asymmetry is the whole design:

    resolution "512": not supported. Accepted: 2K, 4K     <- hard 400
    quality "high" on a model advertising no quality      <- 200, ignored, billed

The first is merely annoying. The second is the one worth a validator: the
control appears to work, the picture comes back, and nothing anywhere says the
setting was dropped.
"""
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.cache import cache
from rest_framework.test import APITestCase

from imagine.services.capabilities import CACHE_KEY
from imagine.validation import DialError, constrain, validate_dials

# Descriptors as `services.catalog` now produces them.
GPT_IMAGE = {
    "id": "openai/gpt-image-2",
    "name": "OpenAI: GPT Image 2",
    "resolutions": [],
    "aspect_ratios": ["1:1", "16:9"],
    "qualities": ["low", "medium", "high"],
    "output_formats": ["png", "jpeg", "webp"],
    "backgrounds": ["auto", "transparent", "opaque"],
    "output_compression": {"min": 0, "max": 100},
    "batch": {"min": 1, "max": 8},
    "max_batch": 8,
    "max_references": 10,
    "supports_seed": False,
}
SEEDREAM = {
    "id": "bytedance-seed/seedream-5-0-lite",
    "name": "Seedream 5.0 Lite",
    "resolutions": ["2K", "4K"],
    "aspect_ratios": ["1:1"],
    "qualities": [],
    "output_formats": [],
    "backgrounds": [],
    "output_compression": None,
    "batch": {"min": 1, "max": 4},
    "max_batch": 4,
    "max_references": 14,
    "supports_seed": True,
}
SEEDANCE = {
    "id": "bytedance/seedance-2.5",
    "name": "Seedance 2.5",
    "resolutions": ["480p", "720p"],
    "aspect_ratios": ["16:9"],
    "sizes": ["1280x720"],
    "durations": [4, 5, 6],
    "frame_slots": ["first_frame", "last_frame"],
    "supports_audio": True,
    "supports_seed": True,
}
VEO = {**SEEDANCE, "id": "google/veo-3.1", "name": "Veo 3.1", "frame_slots": [], "sizes": []}
TTS = {
    "id": "openai/gpt-4o-mini-tts",
    "name": "GPT-4o Mini TTS",
    "voices": ["alloy", "sage"],
    "supports_speed": True,
    "speed_range": {"min": 0.5, "max": 2.0},
    "response_formats": ["mp3", "pcm"],
    "supports_instructions": True,
}
KOKORO = {**TTS, "id": "hexgrad/kokoro-82m", "name": "Kokoro 82M", "voices": [],
          "supports_instructions": False}


class DialValidationTests(APITestCase):
    def assertRejects(self, kind, model, data, field):
        with self.assertRaises(DialError) as caught:
            validate_dials(kind, model, data)
        self.assertIn(field, caught.exception.args[0])
        return caught.exception.args[0][field]

    # ── image ────────────────────────────────────────────────────────────────

    def test_a_value_outside_the_advertised_enum_is_refused_with_the_list(self):
        """The message names what *is* accepted, because the provider's own
        does and the user cannot see the catalogue."""
        message = self.assertRejects('image', SEEDREAM, {'resolution': '1K'}, 'resolution')
        self.assertIn('2K', message)
        self.assertIn('4K', message)

    def test_a_dial_the_model_never_advertises_is_refused(self):
        """This is the silent case: OpenRouter answers 200 and ignores it."""
        self.assertRejects('image', SEEDREAM, {'quality': 'high'}, 'quality')
        self.assertRejects('image', GPT_IMAGE, {'resolution': '2K'}, 'resolution')

    def test_the_dials_the_model_does_advertise_pass(self):
        validate_dials('image', GPT_IMAGE, {
            'aspect_ratio': '16:9', 'quality': 'high', 'background': 'transparent',
            'output_format': 'webp', 'output_compression': 80, 'batch_size': 4,
            'reference_urls': ['https://example.com/a.png'],
        })

    def test_compression_needs_a_lossy_format(self):
        """png plus compression is accepted upstream and does nothing."""
        self.assertRejects('image', GPT_IMAGE,
                           {'output_format': 'png', 'output_compression': 50},
                           'output_compression')

    def test_a_batch_beyond_what_the_model_returns_is_refused(self):
        self.assertRejects('image', GPT_IMAGE, {'batch_size': 20}, 'batch_size')
        self.assertRejects('image', {**GPT_IMAGE, 'batch': None},
                           {'batch_size': 2}, 'batch_size')

    def test_reference_images_are_capped_and_scheme_checked(self):
        self.assertRejects('image', GPT_IMAGE,
                           {'reference_urls': ['https://x/a.png'] * 11}, 'reference_urls')
        self.assertRejects('image', GPT_IMAGE,
                           {'reference_urls': ['file:///etc/passwd']}, 'reference_urls')
        self.assertRejects('image', {**GPT_IMAGE, 'max_references': 0},
                           {'reference_urls': ['https://x/a.png']}, 'reference_urls')
        validate_dials('image', GPT_IMAGE, {'reference_urls': ['data:image/png;base64,aGk=']})

    # ── video ────────────────────────────────────────────────────────────────

    def test_duration_is_matched_against_the_advertised_lengths(self):
        """Stored as a string, advertised as numbers — a comparison that fails
        to bridge that would refuse every valid duration."""
        validate_dials('video', SEEDANCE, {'duration': '6'})
        self.assertRejects('video', SEEDANCE, {'duration': '7'}, 'duration')

    def test_frames_are_refused_where_the_model_has_no_slot(self):
        validate_dials('video', SEEDANCE, {
            'frame_images': [{'url': 'https://x/a.png', 'frame_type': 'first_frame'}],
        })
        self.assertRejects('video', VEO, {
            'frame_images': [{'url': 'https://x/a.png', 'frame_type': 'first_frame'}],
        }, 'frame_images')
        self.assertRejects('video', SEEDANCE, {
            'frame_images': [{'url': 'https://x/a.png', 'frame_type': 'middle'}],
        }, 'frame_images')
        self.assertRejects('video', SEEDANCE, {
            'frame_images': [
                {'url': 'https://x/a.png', 'frame_type': 'first_frame'},
                {'url': 'https://x/b.png', 'frame_type': 'first_frame'},
            ],
        }, 'frame_images')

    # ── audio ────────────────────────────────────────────────────────────────

    def test_an_empty_voice_list_means_free_form_not_forbidden(self):
        """The one dial where empty means the opposite of everywhere else: the
        MiniMax and Kokoro voices are provider ids no endpoint enumerates."""
        validate_dials('audio', KOKORO, {'voice': 'af_bella'})
        self.assertRejects('audio', TTS, {'voice': 'af_bella'}, 'voice')

    def test_speed_is_held_to_the_endpoints_range(self):
        validate_dials('audio', TTS, {'speed': 1.5})
        self.assertRejects('audio', TTS, {'speed': 3.0}, 'speed')

    def test_instructions_are_refused_where_they_would_be_dropped(self):
        validate_dials('audio', TTS, {'instructions': 'warm and unhurried'})
        self.assertRejects('audio', KOKORO, {'instructions': 'warm'}, 'instructions')

    def test_an_unreachable_catalogue_validates_nothing(self):
        """An OpenRouter outage must not become a validation error about the
        user's own choices."""
        validate_dials('image', None, {'resolution': 'nonsense'})


class DialsThroughTheApiTests(APITestCase):
    """The same rule, where the user meets it."""

    CAPS = {"image": [GPT_IMAGE, SEEDREAM], "video": [SEEDANCE], "audio": [TTS]}

    def setUp(self):
        cache.delete(CACHE_KEY)
        self.user = get_user_model().objects.create_user(
            username="dials", email="d@example.com", password="pw",
        )
        self.client.force_authenticate(self.user)

    def _post(self, body):
        with patch("imagine.serializers.capabilities_for", return_value=self.CAPS), \
             patch("imagine.views.OpenRouterService.for_user"), \
             patch("imagine.views.run_generation") as run:
            response = self.client.post("/api/imagine/", body, format="json")
        return response, run

    def test_an_unsupported_dial_is_refused_before_anything_is_billed(self):
        response, run = self._post({
            "type": "image", "prompt": "a fox",
            "model": "bytedance-seed/seedream-5-0-lite", "resolution": "1K",
        })
        self.assertEqual(response.status_code, 400)
        self.assertIn("2K", str(response.json()["resolution"]))
        run.assert_not_called()

    def test_the_full_dial_set_round_trips(self):
        response, run = self._post({
            "type": "image", "prompt": "a fox", "model": "openai/gpt-image-2",
            "aspect_ratio": "16:9", "quality": "high", "background": "transparent",
            "output_format": "webp", "output_compression": 70, "batch_size": 3,
            "reference_urls": ["https://example.com/ref.png"],
        })
        self.assertEqual(response.status_code, 201, response.data)
        body = response.json()
        self.assertEqual(body["background"], "transparent")
        self.assertEqual(body["output_compression"], 70)
        self.assertEqual(body["batch_size"], 3)
        self.assertEqual(body["reference_urls"], ["https://example.com/ref.png"])
        run.assert_called_once()

    def test_outputs_are_read_only_on_the_wire(self):
        """A client that could set `output_urls` could put anything in the
        gallery — including a url this server would later fetch."""
        response, _ = self._post({
            "type": "image", "prompt": "a fox", "model": "openai/gpt-image-2",
            "output_urls": ["https://evil.example/x.png"],
            "output_url": "https://evil.example/x.png",
        })
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.json()["output_urls"], [])


class ConstrainTests(APITestCase):
    """The drop policy: same table, for the caller that must not refuse.

    The conversational path cannot answer a request with a validation error —
    a router that guessed `quality: high` for a model with no quality switch
    has not made the request impossible, it has added something to drop. What
    matters is that it drops by reading the *same* descriptors the form path
    refuses on, so the two cannot disagree about what a model takes.
    """

    def test_it_drops_what_the_model_does_not_advertise(self):
        out = constrain('image', SEEDREAM, {
            'resolution': '2K', 'quality': 'high', 'background': 'transparent',
        })
        self.assertEqual(out, {'resolution': '2K'})

    def test_it_drops_a_value_outside_the_enum_and_keeps_the_rest(self):
        out = constrain('image', SEEDREAM, {'resolution': '1K', 'aspect_ratio': '1:1'})
        self.assertEqual(out, {'aspect_ratio': '1:1'})

    def test_dials_from_another_modality_do_not_travel(self):
        """A router told 'make it a video' keeps the voice it picked a turn
        earlier, and a voice on a video request is a provider error."""
        out = constrain('video', SEEDANCE, {'voice': 'alloy', 'duration': 4})
        self.assertEqual(out, {'duration': 4})

    def test_a_number_is_clamped_rather_than_dropped(self):
        """Asking eight images of a model that returns four is a request for as
        many as it can give, not a request to be ignored."""
        self.assertEqual(constrain('image', SEEDREAM, {'batch_size': 9})['batch_size'], 4)
        self.assertEqual(constrain('audio', TTS, {'speed': 9.0})['speed'], 2.0)

    def test_duration_bridges_string_storage_and_numeric_advertisement(self):
        self.assertEqual(constrain('video', SEEDANCE, {'duration': '5'})['duration'], 5)
        self.assertNotIn('duration', constrain('video', SEEDANCE, {'duration': '7'}))

    def test_a_free_form_voice_survives_where_none_are_enumerated(self):
        self.assertEqual(constrain('audio', KOKORO, {'voice': 'af_bella'})['voice'], 'af_bella')

    def test_frames_and_references_are_filtered_not_refused(self):
        out = constrain('video', SEEDANCE, {'frame_images': [
            {'url': 'https://x/a.png', 'frame_type': 'first_frame'},
            {'url': 'file:///etc/passwd', 'frame_type': 'last_frame'},
            {'url': 'https://x/c.png', 'frame_type': 'middle'},
        ]})
        self.assertEqual(out['frame_images'],
                         [{'url': 'https://x/a.png', 'frame_type': 'first_frame'}])
        out = constrain('image', GPT_IMAGE, {'reference_urls': ['file:///etc/passwd']})
        self.assertNotIn('reference_urls', out)

    def test_compression_follows_the_format_here_too(self):
        self.assertNotIn('output_compression', constrain('image', GPT_IMAGE, {
            'output_format': 'png', 'output_compression': 50,
        }))
        self.assertEqual(constrain('image', GPT_IMAGE, {
            'output_format': 'webp', 'output_compression': 50,
        })['output_compression'], 50)

    def test_the_two_policies_agree_on_what_is_allowed(self):
        """Whatever `constrain` keeps, `validate_dials` must accept — otherwise
        the conversational path builds a row the form path would have refused,
        and the difference only shows up as a provider error."""
        proposed = {
            'resolution': '4K', 'aspect_ratio': '1:1', 'quality': 'high',
            'background': 'transparent', 'output_format': 'webp',
            'output_compression': 200, 'batch_size': 99,
            'reference_urls': ['https://x/a.png'],
        }
        for model in (GPT_IMAGE, SEEDREAM):
            kept = constrain('image', model, proposed)
            validate_dials('image', model, kept)  # must not raise
