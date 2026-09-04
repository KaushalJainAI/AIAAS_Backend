"""OpenRouter API client.

Auth keys come from the per-user encrypted `credentials/` vault via
`credentials.resolution` — the same resolver the chat and node stacks use, so
"usable credential" means one thing product-wide. This module used to run its
own vault query which omitted the `is_verified` filter the rest of the system
applied; media generation would therefore use a key chat had already rejected.
Construct the service via `OpenRouterService.for_user(user)`.

Endpoint reference:
- Image  : POST /api/v1/images             → data[].b64_json
- Video  : POST /api/v1/videos             → polling job (id, polling_url)
- Poll   : GET  /api/v1/videos/{id}        → status, unsigned_urls
- TTS    : POST /api/v1/audio/speech       → raw audio bytes
- Catalog: GET  /api/v1/images/models, GET /api/v1/videos/models

Image generation used to ride on `chat/completions` with
`modalities=["image","text"]`. That path still works, but it can only reach the
~10 image models that are *also* chat models (Gemini and GPT Image). FLUX,
Seedream, Recraft, Krea, Qwen Image, MAI-Image and Riverflow are not chat
models at all and are reachable only through `POST /images` — two thirds of the
catalog was unaddressable. The unified endpoint also names its fields properly
(`resolution`, not `image_config.image_size`) and adds `quality`,
`output_format` and `n`.
"""
import base64
import logging
from typing import Any, Dict, List, Optional

import requests

from credentials.resolution import CredentialUnavailable, resolve_api_key_sync

from . import catalog

logger = logging.getLogger(__name__)


#: The one sentence the whole app says about a missing key. It used to be
#: assembled by appending this file's advice to `CredentialUnavailable`'s own,
#: which already ends in "Add one under Credentials" — so the banner read
#: "…Add one under Credentials, or configure a platform key. Add an
#: 'OpenRouter API' credential under Credentials." Two instructions for one
#: action reads as two actions.
MISSING_CREDENTIAL_MESSAGE = (
    "Imagine needs an OpenRouter key. Add an 'OpenRouter API' credential "
    "under Credentials — generations work immediately, no restart needed."
)

#: Machine-readable companion, so a client can tell this apart from every other
#: 400 without matching on prose. `useImagineStudio` treated *any* 400 from the
#: catalogue as a missing credential, which was true only because nothing else
#: there answers 400 yet.
MISSING_CREDENTIAL_CODE = "credential_missing"


def _image_url(entry: Dict[str, Any]) -> Optional[str]:
    """One `data[]` entry -> something an <img> can show.

    The documented shape is base64 plus a media type; some providers answer
    with a hosted url instead, so both are accepted rather than failing on a
    response that plainly contains an image.
    """
    if not isinstance(entry, dict):
        return None
    b64 = entry.get("b64_json")
    if b64:
        return f"data:{entry.get('media_type') or 'image/png'};base64,{b64}"
    return entry.get("url")


class MissingOpenRouterCredentialError(RuntimeError):
    """Raised when a user has no usable OpenRouter credential configured.

    Kept as its own type because the imagine views and tasks catch it to return
    a specific "add a credential" message; it wraps the shared
    `CredentialUnavailable` rather than being raised from a private query.
    """


class OpenRouterService:
    BASE_URL = "https://openrouter.ai/api/v1"

    # Terminal failure states returned by the video polling endpoint.
    _VIDEO_FAILURE_STATES = {"failed", "cancelled", "expired"}
    _VIDEO_PENDING_STATES = {"pending", "in_progress"}

    def __init__(self, api_key: str):
        if not api_key:
            raise MissingOpenRouterCredentialError(MISSING_CREDENTIAL_MESSAGE)
        self._api_key = api_key

    # ── construction ─────────────────────────────────────────────────────────

    @classmethod
    def for_user(cls, user) -> "OpenRouterService":
        """Build an instance using the user's OpenRouter credential."""
        try:
            api_key = resolve_api_key_sync('openrouter', user.id)
        except CredentialUnavailable as exc:
            # The resolver's own wording is for a developer reading a log; the
            # user gets one instruction naming one place.
            raise MissingOpenRouterCredentialError(
                MISSING_CREDENTIAL_MESSAGE) from exc
        return cls(api_key=api_key)

    @property
    def api_key(self) -> str:
        """The resolved key, for callers that drive their own HTTP client.

        Exposed deliberately: the intent classifier needs the key to hand to
        litellm and was reaching into `_api_key` to get it, which made a
        private attribute part of the contract without saying so.
        """
        return self._api_key

    # ── transport ────────────────────────────────────────────────────────────

    def _headers(self, *, json_body: bool = True) -> Dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "HTTP-Referer": "https://better-n8n.com",
            "X-Title": "Better n8n Imagine",
        }
        if json_body:
            headers["Content-Type"] = "application/json"
        return headers

    # ── models catalog ───────────────────────────────────────────────────────

    def _fetch_catalog(self, path: str) -> List[Dict[str, Any]]:
        """GET a catalog endpoint, returning [] on any failure.

        Each modality is fetched independently so that one endpoint being down
        degrades a single tab rather than blanking the whole picker.
        """
        try:
            response = requests.get(
                f"{self.BASE_URL}/{path}",
                headers=self._headers(json_body=False),
                timeout=15,
            )
            response.raise_for_status()
            data = response.json().get("data")
            return data if isinstance(data, list) else []
        except Exception as e:
            logger.error(f"OpenRouter catalog fetch failed for /{path}: {e}")
            return []

    def fetch_models(self) -> Dict[str, List[Dict[str, Any]]]:
        """Build the per-modality capability catalog.

        Image and video come from their dedicated catalog endpoints, which
        advertise each model's own resolution/aspect-ratio/duration enums — so
        the UI can offer exactly what the selected model accepts instead of one
        hardcoded list that was wrong for most of them. Audio is curated in
        `catalog.TTS_MODELS`; OpenRouter exposes no TTS discovery endpoint.
        """
        images = [
            catalog.normalize_image_model(m) for m in self._fetch_catalog("images/models")
        ]
        videos = [
            catalog.normalize_video_model(m) for m in self._fetch_catalog("videos/models")
        ]

        return {
            "image": catalog.sort_by_recommendation("image", images),
            "video": catalog.sort_by_recommendation("video", videos),
            "audio": catalog.sort_by_recommendation("audio", catalog.audio_catalog()),
        }

    # ── image ────────────────────────────────────────────────────────────────

    def generate_image(
        self, prompt: str, model: str, config: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Image generation via the unified POST /images endpoint.

        Only keys the caller actually set are sent. Omitting a parameter lets
        OpenRouter apply the model's own default, which is strictly better than
        guessing: models disagree about which resolutions they accept (Seedream
        5.0 Lite is 2K/4K only), so a blanket default is wrong somewhere.
        """
        payload: Dict[str, Any] = {"model": model, "prompt": prompt}

        # A negative prompt has no first-class field on this endpoint; models
        # honour it inline, which is also how the chat path expressed it.
        if config.get("negative_prompt"):
            payload["prompt"] = (
                f"{prompt}\n\nAvoid the following: {config['negative_prompt']}"
            )

        # The complete dial set `POST /images` accepts. Each is sent only when
        # the caller set it, and the caller may only set what the *model*
        # claims to support (`serializers.py` refuses the rest): a value outside
        # a model's advertised enum is a hard 400 from OpenRouter, and a key the
        # model never advertised is silently dropped, which is worse — the
        # control looks like it worked.
        for key in ("resolution", "aspect_ratio", "size", "quality",
                    "output_format", "background"):
            if config.get(key):
                payload[key] = config[key]
        if config.get("seed") is not None:
            payload["seed"] = config["seed"]
        if config.get("output_compression") is not None:
            payload["output_compression"] = int(config["output_compression"])
        if config.get("n"):
            payload["n"] = int(config["n"])
        if config.get("reference_urls"):
            # Image-to-image. The endpoint takes urls or data URIs, and they are
            # passed through rather than fetched here, so nothing in this process
            # ever downloads a URL a user pasted.
            payload["input_references"] = list(config["reference_urls"])

        try:
            response = requests.post(
                f"{self.BASE_URL}/images",
                headers=self._headers(),
                json=payload,
                timeout=120,
            )
            response.raise_for_status()
            data = response.json()

            images = data.get("data") or []
            if not images:
                return {"error": "No images generated"}

            # `n` may return several. Reading only `data[0]` silently discarded
            # everything past the first — asked for, generated, billed, and
            # thrown away. `url` stays the first, for readers that show one.
            urls = [u for u in (_image_url(entry) for entry in images) if u]
            if not urls:
                return {"error": "Image response contained no image data"}

            return {
                "status": "completed",
                "url": urls[0],
                "urls": urls,
                "cost": (data.get("usage") or {}).get("cost"),
            }
        except requests.HTTPError as e:
            body = getattr(e.response, "text", "")[:500]
            logger.error(f"OpenRouter image HTTP error: {e} | body: {body}")
            return {"error": f"{e} | {body}" if body else str(e)}
        except Exception as e:
            logger.exception("OpenRouter image generation error")
            return {"error": str(e)}

    # ── video ────────────────────────────────────────────────────────────────

    def generate_video(
        self, prompt: str, model: str, config: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Submit a POST /videos job. Returns id + polling_url."""
        payload: Dict[str, Any] = {"model": model, "prompt": prompt}

        # `POST /videos` has no negative-prompt field, so the same inline
        # treatment the image path uses. It used to be dropped here in silence:
        # the panel offered the box for video, and nothing ever read it.
        if config.get("negative_prompt"):
            payload["prompt"] = (
                f"{prompt}\n\nAvoid the following: {config['negative_prompt']}"
            )

        for key in ("resolution", "aspect_ratio", "size"):
            if config.get(key):
                payload[key] = config[key]
        if config.get("duration"):
            payload["duration"] = int(config["duration"])
        if config.get("generate_audio") is not None:
            payload["generate_audio"] = bool(config["generate_audio"])
        if config.get("seed") is not None:
            payload["seed"] = config["seed"]
        if config.get("reference_urls"):
            payload["input_references"] = [
                {"type": "image_url", "image_url": url}
                for url in config["reference_urls"]
            ]
        if config.get("frame_images"):
            # Image-to-video: pin the first and/or last frame. Which slots a
            # model accepts is advertised as `supported_frame_images`, and the
            # serializer refuses one it does not.
            payload["frame_images"] = [
                {"type": "image_url",
                 "image_url": frame["url"],
                 "frame_type": frame["frame_type"]}
                for frame in config["frame_images"]
            ]

        try:
            response = requests.post(
                f"{self.BASE_URL}/videos",
                headers=self._headers(),
                json=payload,
                timeout=30,
            )
            response.raise_for_status()
            data = response.json()
            return {
                "status": "pending",
                "job_id": data.get("id"),
                "polling_url": data.get("polling_url"),
            }
        except requests.HTTPError as e:
            body = getattr(e.response, "text", "")[:500]
            logger.error(f"OpenRouter video HTTP error: {e} | body: {body}")
            return {"error": f"{e} | {body}" if body else str(e)}
        except Exception as e:
            logger.exception("OpenRouter video generation error")
            return {"error": str(e)}

    def poll_video_status(self, job_id: str) -> Dict[str, Any]:
        """Poll GET /videos/{id}. Maps OpenRouter statuses to our internal ones."""
        try:
            response = requests.get(
                f"{self.BASE_URL}/videos/{job_id}",
                headers=self._headers(json_body=False),
                timeout=10,
            )
            response.raise_for_status()
            data = response.json()

            status = (data.get("status") or "").lower()

            if status == "completed":
                urls = data.get("unsigned_urls") or []
                return {
                    "status": "completed",
                    "url": urls[0] if urls else None,
                }
            if status in self._VIDEO_FAILURE_STATES:
                return {
                    "status": "failed",
                    "error": data.get("error") or f"Video job {status}",
                }
            if status in self._VIDEO_PENDING_STATES:
                return {"status": "pending"}

            # Unknown status — treat as pending so the worker keeps polling
            # rather than erroneously marking a live job failed.
            logger.warning(f"Unknown video status '{status}' for job {job_id}")
            return {"status": "pending"}

        except Exception as e:
            logger.error(f"OpenRouter video polling error: {e}")
            return {"status": "error", "error": str(e)}

    # ── audio (TTS) ──────────────────────────────────────────────────────────

    def generate_audio(
        self, text: str, model: str, config: Dict[str, Any]
    ) -> Dict[str, Any]:
        """TTS via POST /audio/speech. Returns base64 data URL."""
        # response_format default on OpenRouter is `pcm` — we explicitly ask
        # for mp3 so the frontend gets a directly playable audio element.
        response_format = config.get("response_format") or "mp3"
        payload: Dict[str, Any] = {
            "model": model,
            "input": text,
            "response_format": response_format,
        }
        # Voice ids are provider-specific — `alloy` is an OpenAI name and is
        # rejected by MiniMax/Voxtral/Kokoro. Send only what the caller chose
        # and let the model fall back to its own default otherwise.
        if config.get("voice"):
            payload["voice"] = config["voice"]
        if config.get("speed") is not None:
            payload["speed"] = float(config["speed"])
        if config.get("instructions"):
            # Tone direction, an OpenAI-family extra. It rides `provider`,
            # which is how this endpoint carries per-provider options — sending
            # it top-level is rejected by every other provider.
            payload["provider"] = {
                **(payload.get("provider") or {}),
                "options": {"openai": {"instructions": config["instructions"]}},
            }

        try:
            response = requests.post(
                f"{self.BASE_URL}/audio/speech",
                headers=self._headers(),
                json=payload,
                timeout=30,
            )
            response.raise_for_status()
            audio_b64 = base64.b64encode(response.content).decode("utf-8")
            # The data URI has to name what the bytes actually are. `pcm` is
            # raw samples with no container — no <audio> element will play it,
            # and labelling it audio/mpeg produces a player that silently fails
            # on a file that downloaded perfectly. It is still offered, because
            # it is what the endpoint returns by default and what anyone
            # post-processing the audio wants; it just arrives as a download.
            media_type = (
                "application/octet-stream" if response_format == "pcm" else "audio/mpeg"
            )
            return {
                "status": "completed",
                "url": f"data:{media_type};base64,{audio_b64}",
            }
        except requests.HTTPError as e:
            body = getattr(e.response, "text", "")[:500]
            logger.error(f"OpenRouter audio HTTP error: {e} | body: {body}")
            return {"error": f"{e} | {body}" if body else str(e)}
        except Exception as e:
            logger.exception("OpenRouter audio generation error")
            return {"error": str(e)}
