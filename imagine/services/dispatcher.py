"""Shared generation dispatcher used by both the form ViewSet and the agent."""
import logging
import re
from typing import Optional

from django.conf import settings

from ..models import Generation
from .events import broadcast_generation
from .openrouter import MissingOpenRouterCredentialError, OpenRouterService

logger = logging.getLogger(__name__)


def _map_resolution_to_size(res_str: Optional[str]) -> Optional[str]:
    """Coerce a free-form resolution string to OpenRouter's image enum.

    Returns None for an unset value so the key can be dropped entirely — image
    models disagree on which resolutions they accept, so defaulting to "1K"
    here would send an unsupported value to the several models that only offer
    2K/4K.
    """
    if not res_str:
        return None
    s = str(res_str).upper()
    for token in ("4K", "2K", "1K"):
        if token in s:
            return token
    if "4096" in s:
        return "4K"
    if "2048" in s:
        return "2K"
    if "512" in s:
        return "512"
    if "1024" in s:
        return "1K"
    return None


def _normalize_video_resolution(res_str: Optional[str]) -> Optional[str]:
    """Coerce a resolution string into one of OpenRouter's video enums."""
    if not res_str:
        return None
    s = str(res_str).lower().strip()
    for known in ("480p", "720p", "768p", "1080p"):
        if known in s:
            return known
    if "2160" in s:
        return "4K"
    if "1440" in s:
        return "2K"
    for token in ("4k", "2k", "1k"):
        if token in s:
            return token.upper()
    return None


def _parse_seconds(value, default: Optional[int] = None) -> Optional[int]:
    """Accept '5', '5s', '10 seconds', 5, 1.5, etc. Returns default on garbage.

    A naive digit filter turned "1.5" into 15; keep the decimal point when
    parsing so fractional durations round instead of multiplying by ten.
    """
    if value is None or value == "":
        return default
    if isinstance(value, (int, float)):
        return int(value)
    match = re.search(r"\d+(?:\.\d+)?", str(value))
    if not match:
        return default
    return int(float(match.group()))


def _build_config(generation: Generation) -> dict:
    """Assemble the provider config, dropping anything the user never set.

    Every key here used to be emitted unconditionally, so an unset field
    arrived as an explicit `None` — and `config.get("aspect_ratio", "1:1")`
    returns `None`, not the default, when the key is present. Null aspect
    ratios were being posted to OpenRouter as a result. Absent means absent
    now, and the model's own default applies.
    """
    config = {
        "aspect_ratio": generation.aspect_ratio,
        "negative_prompt": generation.negative_prompt,
        "seed": generation.seed,
        "voice": generation.voice,
        "speed": generation.speed,
        "quality": generation.quality,
        "output_format": generation.output_format,
        "generate_audio": generation.generate_audio,
    }

    if generation.type == "video":
        config["resolution"] = _normalize_video_resolution(generation.resolution)
        config["duration"] = _parse_seconds(generation.duration)
    else:
        config["resolution"] = _map_resolution_to_size(generation.resolution)

    return {k: v for k, v in config.items() if v is not None and v != ""}


def run_image_generation(generation: Generation, service: OpenRouterService) -> None:
    """Execute the image call and write the outcome to the row.

    Shared by the inline dispatch and the Celery worker so the two paths cannot
    drift — the worker must produce exactly what the request cycle would have.
    """
    result = service.generate_image(
        generation.prompt, generation.model, _build_config(generation)
    )
    if "error" in result:
        generation.status = "failed"
        generation.error_message = result["error"]
    else:
        generation.status = "completed"
        generation.output_url = result["url"]
        if result.get("cost") is not None:
            generation.metadata = {
                **(generation.metadata or {}),
                "cost_usd": result["cost"],
            }
    generation.save()


def run_generation(generation: Generation) -> Generation:
    """Dispatch a Generation to OpenRouter. Mutates and saves the row in place.

    Image is async only when `RUN_WORKFLOWS_ASYNC` is on: the row is left
    `pending` and a Celery worker performs the call, exactly like the video
    path. Local dev and tests run without Redis, so they stay inline — the same
    split as `inference.dispatch_extraction`. If the broker is unreachable at
    enqueue time the call falls back to inline rather than leaving a `pending`
    row that can never complete.
    """
    _broadcast_started(generation)

    try:
        service = OpenRouterService.for_user(generation.user)
    except MissingOpenRouterCredentialError as e:
        generation.status = "failed"
        generation.error_message = str(e)
        generation.save()
        broadcast_generation(generation, "generation.failed")
        return generation

    try:
        if generation.type == "image":
            if settings.RUN_WORKFLOWS_ASYNC:
                _enqueue_image(generation, service)
            else:
                run_image_generation(generation, service)

        elif generation.type == "video":
            result = service.generate_video(generation.prompt, generation.model, _build_config(generation))
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
            result = service.generate_audio(generation.prompt, generation.model, _build_config(generation))
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
        broadcast_generation(
            generation,
            "generation.completed" if generation.status == "completed" else "generation.failed",
        )
    return generation


def _broadcast_started(generation: Generation) -> None:
    """Fire `generation.started` before anything else touches the row."""
    broadcast_generation(generation, "generation.started", prompt=generation.prompt)


def _enqueue_image(generation: Generation, service: OpenRouterService) -> None:
    """Offload the image call to the worker, falling back to inline.

    A `pending` row with no worker behind it is a worse failure than a slow
    request — if the task cannot be enqueued the request cycle does the work
    so the row always reaches a terminal state.
    """
    generation.status = "pending"
    generation.save()
    try:
        from ..tasks import generate_image_task
        generate_image_task.delay(generation.id)
    except Exception as e:
        logger.warning("Image task enqueue failed (%s); running inline", e)
        generation.status = "processing"
        generation.save()
        run_image_generation(generation, service)