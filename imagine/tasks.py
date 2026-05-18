import logging
from celery import shared_task
from .models import Generation
from .services.openrouter import MissingOpenRouterCredentialError, OpenRouterService

logger = logging.getLogger(__name__)


def _broadcast_generation(generation: Generation, event_type: str) -> None:
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
                    "output_url": generation.output_url,
                    "error": generation.error_message,
                },
            },
        )
    except Exception as e:
        logger.debug(f"WS broadcast skipped: {e}")


@shared_task(bind=True, max_retries=20)
def poll_video_generation(self, generation_id):
    """Poll OpenRouter for video generation status."""
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
            _broadcast_generation(gen, "generation.failed")
            return

        result = service.poll_video_status(gen.job_id)

        if result["status"] == "completed":
            gen.status = "completed"
            gen.output_url = result["url"]
            gen.save()
            _broadcast_generation(gen, "generation.completed")
        elif result["status"] == "failed":
            gen.status = "failed"
            gen.error_message = result.get("error", "Generation failed")
            gen.save()
            _broadcast_generation(gen, "generation.failed")
        elif result["status"] in ("pending", "in_progress"):
            _broadcast_generation(gen, "generation.progress")
            raise self.retry(countdown=30)

    except Generation.DoesNotExist:
        logger.error(f"Generation {generation_id} not found")
    except Exception as e:
        logger.error(f"Error polling video status for {generation_id}: {e}")
        raise self.retry(countdown=60)
