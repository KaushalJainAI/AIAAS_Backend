"""Shared generation dispatcher used by both the form ViewSet and the agent."""
import logging
from typing import Optional

from ..models import Generation
from .openrouter import MissingOpenRouterCredentialError, OpenRouterService

logger = logging.getLogger(__name__)


def _map_resolution_to_size(res_str: Optional[str]) -> str:
    """Coerce a free-form resolution string to OpenRouter's image_size enum."""
    if not res_str:
        return "1K"
    s = res_str.upper()
    if "4096" in s or "4K" in s:
        return "4K"
    if "2048" in s or "2K" in s:
        return "2K"
    return "1K"


def _normalize_video_resolution(res_str: Optional[str]) -> str:
    """Coerce a resolution string into one of OpenRouter's video enums."""
    if not res_str:
        return "720p"
    s = res_str.lower().strip()
    for known in ("480p", "720p", "1080p"):
        if known in s:
            return known
    if "4k" in s or "2160" in s:
        return "4K"
    if "2k" in s or "1440" in s:
        return "2K"
    if "1k" in s:
        return "1K"
    return "720p"


def _parse_seconds(value, default: int = 5) -> int:
    """Accept '5', '5s', '10 seconds', 5, etc. Falls back to default on garbage."""
    if value is None or value == "":
        return default
    if isinstance(value, (int, float)):
        return int(value)
    digits = "".join(c for c in str(value) if c.isdigit())
    return int(digits) if digits else default


def _build_config(generation: Generation) -> dict:
    return {
        "aspect_ratio": generation.aspect_ratio,
        "image_size": _map_resolution_to_size(generation.resolution),
        "resolution": _normalize_video_resolution(generation.resolution),
        "duration": _parse_seconds(generation.duration, default=5),
        "negative_prompt": generation.negative_prompt,
        "seed": generation.seed,
        "voice": generation.voice,
        "speed": generation.speed,
    }


def _broadcast(generation: Generation, event_type: str, payload: dict) -> None:
    """Push a status event to the user's imagine WS group. No-op if Channels not configured."""
    try:
        from channels.layers import get_channel_layer
        from asgiref.sync import async_to_sync
        layer = get_channel_layer()
        if not layer:
            return
        async_to_sync(layer.group_send)(
            f"imagine_agent_{generation.user_id}",
            {
                "type": "imagine.event",
                "event": event_type,
                "data": {
                    "generation_id": generation.id,
                    "status": generation.status,
                    "type": generation.type,
                    **payload,
                },
            },
        )
    except Exception as e:
        logger.debug(f"WS broadcast skipped: {e}")


def run_generation(generation: Generation) -> Generation:
    """Dispatch a Generation to OpenRouter. Mutates and saves the row in place."""
    config = _build_config(generation)
    _broadcast(generation, "generation.started", {"prompt": generation.prompt})

    try:
        service = OpenRouterService.for_user(generation.user)
    except MissingOpenRouterCredentialError as e:
        generation.status = "failed"
        generation.error_message = str(e)
        generation.save()
        _broadcast(generation, "generation.failed", {
            "output_url": None,
            "error": generation.error_message,
        })
        return generation

    try:
        if generation.type == "image":
            result = service.generate_image(generation.prompt, generation.model, config)
            if "error" in result:
                generation.status = "failed"
                generation.error_message = result["error"]
            else:
                generation.status = "completed"
                generation.output_url = result["url"]
            generation.save()

        elif generation.type == "video":
            result = service.generate_video(generation.prompt, generation.model, config)
            if "error" in result:
                generation.status = "failed"
                generation.error_message = result["error"]
                generation.save()
            else:
                generation.status = "pending"
                generation.job_id = result["job_id"]
                generation.polling_url = result["polling_url"]
                generation.save()
                from ..tasks import poll_video_generation
                poll_video_generation.delay(generation.id)

        elif generation.type == "audio":
            result = service.generate_audio(generation.prompt, generation.model, config)
            if "error" in result:
                generation.status = "failed"
                generation.error_message = result["error"]
            else:
                generation.status = "completed"
                generation.output_url = result["url"]
            generation.save()

        else:
            generation.status = "failed"
            generation.error_message = f"Unsupported type: {generation.type}"
            generation.save()

    except Exception as e:
        logger.exception("run_generation failed")
        generation.status = "failed"
        generation.error_message = str(e)
        generation.save()

    if generation.status in ("completed", "failed"):
        _broadcast(
            generation,
            "generation.completed" if generation.status == "completed" else "generation.failed",
            {"output_url": generation.output_url, "error": generation.error_message},
        )
    return generation
