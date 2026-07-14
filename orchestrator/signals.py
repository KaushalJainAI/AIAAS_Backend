import logging
import redis
from django.db.models.signals import pre_delete
from django.dispatch import receiver
from .models import Workflow
from executor.trigger_manager import get_trigger_manager

logger = logging.getLogger(__name__)

@receiver(pre_delete, sender=Workflow)
def cleanup_workflow_triggers(sender, instance, **kwargs):
    """
    Ensure all background resources (schedules, webhooks, polling)
    are cleaned up when a workflow is deleted.
    """
    try:
        mgr = get_trigger_manager()
        mgr.unregister_triggers(instance.id)
        logger.info(f"Automatically unregistered triggers for deleted workflow {instance.id}")
    except redis.exceptions.RedisError as e:
        # Triggers live in Redis; when Redis isn't running (e.g. this staging
        # box) there is nothing registered to clean up. Skip quietly rather
        # than spamming errors on every delete.
        logger.debug(f"Trigger cleanup skipped for workflow {instance.id} (Redis unavailable): {e}")
    except Exception as e:
        logger.error(f"Failed to cleanup triggers for workflow {instance.id} during deletion: {e}")
