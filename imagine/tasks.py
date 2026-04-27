import time
import logging
from celery import shared_task
from .models import Generation
from .services.openrouter import OpenRouterService

logger = logging.getLogger(__name__)

@shared_task(bind=True, max_retries=20)
def poll_video_generation(self, generation_id):
    """
    Poll OpenRouter for video generation status.
    """
    try:
        gen = Generation.objects.get(id=generation_id)
        if not gen.job_id:
            logger.error(f"Generation {generation_id} has no job_id")
            return

        result = OpenRouterService.poll_video_status(gen.job_id)
        
        if result["status"] == "completed":
            gen.status = "completed"
            gen.output_url = result["url"]
            gen.save()
        elif result["status"] == "failed":
            gen.status = "failed"
            gen.error_message = result.get("error", "Generation failed")
            gen.save()
        elif result["status"] == "pending" or result["status"] == "in_progress":
            # Retry in 30 seconds
            raise self.retry(countdown=30)
            
    except Generation.DoesNotExist:
        logger.error(f"Generation {generation_id} not found")
    except Exception as e:
        logger.error(f"Error polling video status for {generation_id}: {e}")
        raise self.retry(countdown=60)
