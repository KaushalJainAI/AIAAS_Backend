import logging
from celery import shared_task
from celery.exceptions import MaxRetriesExceededError, Retry
from .models import Generation
from .services.dispatcher import run_image_generation
from .services.events import broadcast_generation
from .services.openrouter import MissingOpenRouterCredentialError, OpenRouterService

logger = logging.getLogger(__name__)


@shared_task(bind=True, max_retries=20)
def poll_video_generation(self, generation_id):
    """Poll OpenRouter for video generation status.

    A poll whose HTTP call itself failed is retried, not treated as terminal —
    `poll_video_status` returns `{"status": "error"}` on a transport failure,
    which used to fall through every status branch and strand the row in
    `pending` forever. When the retry budget (20 × 30 s) runs out the row is
    marked failed, so a job still running past it is told apart from a live one
    instead of spinning forever.
    """
    gen = None
    try:
        gen = Generation.objects.get(id=generation_id)
        if not gen.job_id:
            logger.error(f"Generation {generation_id} has no job_id")
            return

        try:
            service = OpenRouterService.for_user(gen.user)
        except MissingOpenRouterCredentialError as e:
            gen.status = "failed"
            gen.error_message = str(e)
            gen.save()
            broadcast_generation(gen, "generation.failed")
            return

        result = service.poll_video_status(gen.job_id)

        if result["status"] == "completed":
            gen.status = "completed"
            gen.output_url = result["url"]
            gen.save()
            # Persist to Documents (Images/Videos/Audio per user)
            try:
                from .services.documents import persist_generation_as_document

                # Need a fresh user relation after save; generation already has user
                persist_generation_as_document(gen)
            except Exception:
                logger.exception("Failed to persist video generation %s as document", generation_id)
            broadcast_generation(gen, "generation.completed")
        elif result["status"] == "failed":
            gen.status = "failed"
            gen.error_message = result.get("error", "Generation failed")
            gen.save()
            broadcast_generation(gen, "generation.failed")
        else:
            # pending / in_progress / error — the last is a transient poll
            # failure, and all three mean "try again". `self.retry` raises
            # `Retry`, which must reach the worker untouched — the `except
            # Retry: raise` below stops it being re-caught as a generic error
            # and re-retried a second time at the wrong countdown.
            if result["status"] == "error":
                logger.warning(
                    "Video poll failed for generation %s: %s",
                    generation_id, result.get("error"),
                )
            broadcast_generation(gen, "generation.progress")
            try:
                raise self.retry(countdown=30)
            except MaxRetriesExceededError:
                gen.status = "failed"
                gen.error_message = (
                    "Video generation still in progress after the polling limit"
                )
                gen.save()
                broadcast_generation(gen, "generation.failed")

    except Retry:
        raise
    except Generation.DoesNotExist:
        logger.error(f"Generation {generation_id} not found")
    except Exception as e:
        logger.error(f"Error polling video status for {generation_id}: {e}")
        if gen is None:
            return
        try:
            raise self.retry(countdown=60)
        except MaxRetriesExceededError:
            gen.status = "failed"
            gen.error_message = str(e) or "Video generation polling failed"
            gen.save()
            broadcast_generation(gen, "generation.failed")


@shared_task(bind=True)
def generate_image_task(self, generation_id):
    """Generate an image off the request cycle (`RUN_WORKFLOWS_ASYNC`).

    Shares `run_image_generation` with the inline dispatch path, so the worker
    produces exactly what the request cycle would have. No retries on purpose:
    the call itself is the billed operation, and retrying a paid generation
    risks double-spending on a transient failure.
    """
    try:
        gen = Generation.objects.get(id=generation_id)
    except Generation.DoesNotExist:
        logger.error(f"Generation {generation_id} not found")
        return

    try:
        service = OpenRouterService.for_user(gen.user)
    except MissingOpenRouterCredentialError as e:
        gen.status = "failed"
        gen.error_message = str(e)
        gen.save()
        broadcast_generation(gen, "generation.failed")
        return

    try:
        run_image_generation(gen, service)
    except Exception as e:
        logger.exception(f"Image generation failed for {generation_id}")
        gen.status = "failed"
        gen.error_message = str(e)
        gen.save()

    broadcast_generation(
        gen,
        "generation.completed" if gen.status == "completed" else "generation.failed",
    )