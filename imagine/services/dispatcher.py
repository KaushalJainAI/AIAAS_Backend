"""Shared generation dispatcher used by both the form ViewSet and the agent."""
import logging
from typing import Optional

from ..models import Generation
from .openrouter import OpenRouterService

logger = logging.getLogger(__name__)


def _map_resolution_to_size(res_str: Optional[str]) -> str:
    if not res_str:
        return "1K"
    s = res_str.upper()
    if "4096" in s or "4K" in s:
        return "4K"
    if "2048" in s or "2K" in s:
        return "2K"
    return "1K"


def _build_config(generation: Generation) -> dict:
    return {
        "aspect_ratio": generation.aspect_ratio,
        "image_size": _map_resolution_to_size(generation.resolution),
        "resolution": generation.resolution,
        "duration": generation.duration,
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
        if generation.type == "image":
            result = OpenRouterService.generate_image(generation.prompt, generation.model, config)
            if "error" in result:
                generation.status = "failed"
                generation.error_message = result["error"]
            else:
                generation.status = "completed"
                generation.output_url = result["url"]
            generation.save()

        elif generation.type == "video":
            result = OpenRouterService.generate_video(generation.prompt, generation.model, config)
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
            result = OpenRouterService.generate_audio(generation.prompt, generation.model, config)
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
