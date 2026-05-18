"""OpenRouter API client.

Auth keys are NEVER read from settings/.env — they come from the per-user
encrypted `credentials/` vault (slug `openrouter`, field `apiKey`). Construct
the service via `OpenRouterService.for_user(user)`.

Endpoint reference (Nov 2025+ docs):
- Image  : POST /api/v1/chat/completions  with modalities=["image","text"]
- Video  : POST /api/v1/videos             → polling job (id, polling_url)
- Poll   : GET  /api/v1/videos/{id}        → status, unsigned_urls
- TTS    : POST /api/v1/audio/speech       → raw audio bytes
- Models : GET  /api/v1/models             → filter by output_modalities
"""
import base64
import logging
from typing import Any, Dict, List, Optional

import requests

logger = logging.getLogger(__name__)


class MissingOpenRouterCredentialError(RuntimeError):
    """Raised when a user has no active OpenRouter credential configured."""


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
        """Build an instance using the user's active OpenRouter credential."""
        from credentials.models import Credential

        cred = (
            Credential.objects
            .select_related('credential_type')
            .filter(
                user=user,
                credential_type__slug='openrouter',
                is_active=True,
            )
            .order_by('-updated_at')
            .first()
        )
        if not cred:
            raise MissingOpenRouterCredentialError(
                "No active OpenRouter credential found for this user. "
                "Add one under Credentials (type: OpenRouter API)."
            )

        data = cred.get_credential_data()
        # The seeded schema uses field name 'apiKey'; accept common variants
        # in case credentials were stored via a different UI revision.
        api_key = (
            data.get('apiKey')
            or data.get('api_key')
            or data.get('token')
        )
        if not api_key:
            raise MissingOpenRouterCredentialError(
                "OpenRouter credential is missing the API key field."
            )
        return cls(api_key=api_key)

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

    def fetch_models(self) -> Dict[str, List[Dict[str, Any]]]:
        """Fetch the model catalog and bucket by output modality."""
        try:
            response = requests.get(
                f"{self.BASE_URL}/models",
                headers=self._headers(json_body=False),
                timeout=10,
            )
            response.raise_for_status()
            all_models = response.json().get("data", [])

            # Video models carry extra resolution/aspect-ratio metadata.
            video_meta: Dict[str, Dict[str, Any]] = {}
            try:
                v_resp = requests.get(
                    f"{self.BASE_URL}/videos/models",
                    headers=self._headers(json_body=False),
                    timeout=10,
                )
                if v_resp.status_code == 200:
                    video_meta = {m['id']: m for m in v_resp.json().get("data", [])}
            except requests.RequestException as e:
                logger.debug(f"Video model metadata fetch skipped: {e}")

            capabilities: Dict[str, List[Dict[str, Any]]] = {
                "image": [], "video": [], "audio": [],
            }

            for model in all_models:
                model_id = model.get("id")
                model_name = model.get("name")
                output_modalities = model.get("output_modalities") or []

                if "image" in output_modalities:
                    capabilities["image"].append({
                        "id": model_id,
                        "name": model_name,
                        "description": model.get("description", ""),
                        "resolutions": ["1K", "2K", "4K"],
                        "aspect_ratios": ["1:1", "16:9", "9:16", "4:3", "3:4"],
                        "parameters": model.get("supported_parameters", []),
                    })

                if "video" in output_modalities:
                    meta = video_meta.get(model_id, {})
                    capabilities["video"].append({
                        "id": model_id,
                        "name": model_name,
                        "description": model.get("description", ""),
                        "resolutions": meta.get(
                            "supported_resolutions",
                            ["480p", "720p", "1080p"],
                        ),
                        "aspect_ratios": meta.get(
                            "supported_aspect_ratios",
                            ["16:9", "9:16", "1:1"],
                        ),
                        "durations": meta.get("supported_durations", [5, 10]),
                        "parameters": model.get("supported_parameters", []),
                    })

                if "audio" in output_modalities or (model_id and "tts" in model_id.lower()):
                    capabilities["audio"].append({
                        "id": model_id,
                        "name": model_name,
                        "description": model.get("description", ""),
                        "voices": ["alloy", "echo", "fable", "onyx", "nova", "shimmer"],
                        "parameters": model.get("supported_parameters", []),
                    })

            return capabilities
        except Exception as e:
            logger.error(f"Error fetching OpenRouter models: {e}")
            return {"image": [], "video": [], "audio": []}

    # ── image ────────────────────────────────────────────────────────────────

    def generate_image(
        self, prompt: str, model: str, config: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Image generation via chat/completions with modalities=['image','text']."""
        messages: List[Dict[str, str]] = []
        if config.get("negative_prompt"):
            messages.append({
                "role": "system",
                "content": f"Negative prompt: {config['negative_prompt']}",
            })
        messages.append({"role": "user", "content": prompt})

        image_config: Dict[str, Any] = {
            "aspect_ratio": config.get("aspect_ratio", "1:1"),
            "image_size": config.get("image_size", "1K"),
        }
        if config.get("strength") is not None:
            image_config["strength"] = config["strength"]

        payload = {
            "model": model,
            "messages": messages,
            "modalities": ["image", "text"],
            "image_config": image_config,
        }

        try:
            response = requests.post(
                f"{self.BASE_URL}/chat/completions",
                headers=self._headers(),
                json=payload,
                timeout=60,
            )
            response.raise_for_status()
            data = response.json()

            choices = data.get("choices") or []
            if not choices:
                return {"error": "No response from model"}

            message = choices[0].get("message") or {}
            images = message.get("images") or []
            if not images:
                return {"error": "No images generated"}

            # Response shape: images[].image_url.url is a base64 data URL.
            first = images[0]
            url = (first.get("image_url") or {}).get("url") or first.get("url")
            if not url:
                return {"error": "Image response missing url"}
            return {"status": "completed", "url": url}
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
        payload: Dict[str, Any] = {
            "model": model,
            "prompt": prompt,
            "resolution": config.get("resolution", "720p"),
            "aspect_ratio": config.get("aspect_ratio", "16:9"),
            "duration": int(config.get("duration") or 5),
        }
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
            "voice": config.get("voice", "alloy"),
            "response_format": "mp3",
        }
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
