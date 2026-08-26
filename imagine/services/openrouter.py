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
from typing import Any, Dict, List

import requests

from credentials.resolution import CredentialUnavailable, resolve_api_key_sync

from . import catalog

logger = logging.getLogger(__name__)


class MissingOpenRouterCredentialError(RuntimeError):
    """Raised when a user has no usable OpenRouter credential configured.

    Kept as its own type because the imagine views and tasks catch it to return
    a specific "add a credential" message; it now wraps the shared
    `CredentialUnavailable` rather than being raised from a private query.
    """


class OpenRouterService:
    BASE_URL = "https://openrouter.ai/api/v1"

    # Terminal failure states returned by the video polling endpoint.
    _VIDEO_FAILURE_STATES = {"failed", "cancelled", "expired"}
    _VIDEO_PENDING_STATES = {"pending", "in_progress"}

    def __init__(self, api_key: str):
        if not api_key:
            raise MissingOpenRouterCredentialError(
                "An OpenRouter API key is required. Add an 'OpenRouter API' "
                "credential under Credentials in the app."
            )
        self._api_key = api_key

    # ── construction ─────────────────────────────────────────────────────────

    @classmethod
    def for_user(cls, user) -> "OpenRouterService":
        """Build an instance using the user's OpenRouter credential."""
        try:
            api_key = resolve_api_key_sync('openrouter', user.id)
        except CredentialUnavailable as exc:
            raise MissingOpenRouterCredentialError(
                f"{exc} Add an 'OpenRouter API' credential under Credentials."
            ) from exc
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

        for key in ("resolution", "aspect_ratio", "quality", "output_format", "background"):
            if config.get(key):
                payload[key] = config[key]
        if config.get("seed") is not None:
            payload["seed"] = config["seed"]

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

            first = images[0]
            # Preferred shape is base64 + media type; some providers return a
            # hosted url instead, so accept either rather than failing on a
            # response that plainly contains an image.
            b64 = first.get("b64_json")
            if b64:
                media_type = first.get("media_type") or "image/png"
                url = f"data:{media_type};base64,{b64}"
            else:
                url = first.get("url")
            if not url:
                return {"error": "Image response contained no image data"}

            return {
                "status": "completed",
                "url": url,
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

        for key in ("resolution", "aspect_ratio"):
            if config.get(key):
                payload[key] = config[key]
        if config.get("duration"):
            payload["duration"] = int(config["duration"])
        if config.get("generate_audio") is not None:
            payload["generate_audio"] = bool(config["generate_audio"])
        if config.get("seed") is not None:
            payload["seed"] = config["seed"]

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
        payload: Dict[str, Any] = {
            "model": model,
            "input": text,
            "response_format": "mp3",
        }
        # Voice ids are provider-specific — `alloy` is an OpenAI name and is
        # rejected by MiniMax/Voxtral/Kokoro. Send only what the caller chose
        # and let the model fall back to its own default otherwise.
        if config.get("voice"):
            payload["voice"] = config["voice"]
        if config.get("speed") is not None:
            payload["speed"] = float(config["speed"])

        try:
            response = requests.post(
                f"{self.BASE_URL}/audio/speech",
                headers=self._headers(),
                json=payload,
                timeout=30,
            )
            response.raise_for_status()
            audio_b64 = base64.b64encode(response.content).decode("utf-8")
            return {
                "status": "completed",
                "url": f"data:audio/mpeg;base64,{audio_b64}",
            }
        except requests.HTTPError as e:
            body = getattr(e.response, "text", "")[:500]
            logger.error(f"OpenRouter audio HTTP error: {e} | body: {body}")
            return {"error": f"{e} | {body}" if body else str(e)}
        except Exception as e:
            logger.exception("OpenRouter audio generation error")
            return {"error": str(e)}
