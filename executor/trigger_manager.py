import json
import logging
import redis
from django.conf import settings

from compiler.config_access import get_node_config
from compiler.utils import get_node_type
from workflow_backend.celery import app

logger = logging.getLogger(__name__)

# Trigger node classifications — centralised so adding a new polling/webhook
# trigger is a single-line change.
_WEBHOOK_TRIGGERS = {"webhook_trigger", "github_trigger", "telegram_trigger"}
_POLLING_TRIGGERS = {
    "rss_feed_trigger", "email_trigger", "google_sheets_trigger", "telegram_trigger",
}

class TriggerManager:
    """
    Manages active trigger registrations in Redis and Celery.
    
    This acts as a shared index for all backend processes.
    Redis keys:
      - webhook:{user_id}/{path} -> JSON configuration for incoming webhooks
      - triggers:{workflow_id} -> SET of Redis keys registered for this workflow (for cleanup)
    """

    def __init__(self):
        # Use existing Celery broker URL for Redis connection
        self._redis = redis.from_url(settings.CELERY_BROKER_URL, decode_responses=True)

    def register_triggers(self, workflow):
        """
        Scan workflow nodes and register any trigger nodes found.
        
        Args:
            workflow: The Workflow model instance
        """
        registered_keys = []
        workflow_id = workflow.id
        user_id = workflow.user_id
        
        # Clean up existing triggers first to avoid stale data
        self.unregister_triggers(workflow_id)

        for node in (workflow.nodes or []):
            node_type = get_node_type(node)
            config = get_node_config(node)

            if node_type in _WEBHOOK_TRIGGERS:
                key = self._register_webhook(node, node_type, config, user_id, workflow_id)
                if key:
                    registered_keys.append(key)

            elif node_type == "schedule_trigger":
                self._register_schedule(workflow, config)
                registered_keys.append(f"schedule:{workflow_id}")
                logger.info(f"Registered schedule for workflow {workflow_id}")

            # NB: telegram_trigger appears in both webhook + polling sets; the
            # webhook branch runs first and `continue`s, so polling registration
            # only fires for nodes whose type isn't already webhook-handled.
            if node_type in _POLLING_TRIGGERS and node_type not in _WEBHOOK_TRIGGERS:
                self._setup_periodic_polling(workflow, node["id"], config)
                polling_key = f"polling:{workflow_id}:{node['id']}"
                registered_keys.append(polling_key)
                logger.info(
                    f"Registered polling for {node_type} ({node['id']}) in workflow {workflow_id}"
                )

        # Store keys in a set for easier cleanup later
        if registered_keys:
            cleanup_key = f"triggers:{workflow_id}"
            self._redis.sadd(cleanup_key, *registered_keys)

    def unregister_triggers(self, workflow_id: int):
        """
        Remove all registered triggers for a particular workflow.
        """
        cleanup_key = f"triggers:{workflow_id}"
        keys_to_delete = self._redis.smembers(cleanup_key)
        
        if keys_to_delete:
            self._redis.delete(*keys_to_delete)
            logger.info(f"Cleaned up {len(keys_to_delete)} trigger keys for workflow {workflow_id}")
            
        self._redis.delete(cleanup_key)
        self._unregister_schedule(workflow_id)
        self._unregister_polling(workflow_id)

    def lookup_webhook(self, user_id: int, path: str) -> dict | None:
        """
        Lookup a webhook configuration by user and path.
        """
        path = path.strip("/")
        key = f"webhook:{user_id}/{path}"
        data = self._redis.get(key)
        if data:
            return json.loads(data)
        return None

    def _register_webhook(
        self, node: dict, node_type: str, config: dict,
        user_id: int, workflow_id: int,
    ) -> str | None:
        """
        Register a single webhook trigger in Redis. Returns the key on
        success, or None if the node lacks enough config to register.
        """
        path = (config.get("path") or "").strip("/")
        if not path:
            if node_type == "telegram_trigger":
                path = "telegram"
            elif node_type == "github_trigger" and config.get("repository"):
                path = f"github/{config['repository'].replace('/', '-')}"
            else:
                return None

        key = f"webhook:{user_id}/{path}"
        webhook_config = {
            "workflow_id": workflow_id,
            "user_id": user_id,
            "method": config.get("method", "POST"),
            "authentication": config.get("authentication", "none"),
            "auth_key": config.get("auth_key", ""),
            "auth_value": config.get("auth_value", ""),
            "secret_token": config.get("secret_token", ""),
            "node_type": node_type,
            "node_id": node.get("id"),
        }
        self._redis.set(key, json.dumps(webhook_config))
        logger.info(f"Registered {node_type}: {key} for workflow {workflow_id}")
        return key

    def _register_schedule(self, workflow, config):
        """
        Add a periodic task to Celery Beat.
        """
        from django_celery_beat.models import PeriodicTask, CrontabSchedule, IntervalSchedule
        import json
        
        interval_type = config.get("interval_type", "cron")
        task_name = f"workflow-schedule-{workflow.id}"
        
        try:
            if interval_type == "cron":
                cron_expr = config.get("cron", "0 9 * * *")
                parts = cron_expr.split()
                if len(parts) != 5:
                    logger.error(f"Invalid cron expression: {cron_expr}")
                    return

                schedule, _ = CrontabSchedule.objects.get_or_create(
                    minute=parts[0],
                    hour=parts[1],
                    day_of_month=parts[2],
                    month_of_year=parts[3],
                    day_of_week=parts[4],
                    timezone=config.get("timezone", "UTC")
                )
                PeriodicTask.objects.update_or_create(
                    name=task_name,
                    defaults={
                        'crontab': schedule,
                        'task': 'executor.tasks.execute_scheduled_workflow',
                        'args': json.dumps([workflow.id, workflow.user_id]),
                        'enabled': True
                    }
                )
            else:
                interval_value = int(config.get("interval_value", "30"))
                period = IntervalSchedule.MINUTES
                if interval_type == "hours":
                    period = IntervalSchedule.HOURS
                elif interval_type == "days":
                    period = IntervalSchedule.DAYS
                
                schedule, _ = IntervalSchedule.objects.get_or_create(
                    every=interval_value,
                    period=period
                )
                PeriodicTask.objects.update_or_create(
                    name=task_name,
                    defaults={
                        'interval': schedule,
                        'task': 'executor.tasks.execute_scheduled_workflow',
                        'args': json.dumps([workflow.id, workflow.user_id]),
                        'enabled': True
                    }
                )
        except Exception as e:
            logger.exception(f"Failed to register schedule for workflow {workflow.id}: {e}")

    def _setup_periodic_polling(self, workflow, node_id, config):
        """
        Setup a periodic task for a polling trigger node.
        """
        from django_celery_beat.models import PeriodicTask, IntervalSchedule
        import json
        
        interval_minutes = int(config.get("poll_interval", "15"))
        task_name = f"workflow-polling-{workflow.id}-{node_id}"
        
        try:
            schedule, _ = IntervalSchedule.objects.get_or_create(
                every=interval_minutes,
                period=IntervalSchedule.MINUTES
            )
            PeriodicTask.objects.update_or_create(
                name=task_name,
                defaults={
                    'interval': schedule,
                    'task': 'executor.tasks.poll_workflow_trigger',
                    'args': json.dumps([workflow.id, node_id]),
                    'enabled': True
                }
            )
        except Exception as e:
            logger.exception(f"Failed to setup polling for workflow {workflow.id} node {node_id}: {e}")

    def _unregister_schedule(self, workflow_id: int):
        """
        Remove the periodic task from Celery Beat.
        """
        from django_celery_beat.models import PeriodicTask
        task_name = f"workflow-schedule-{workflow_id}"
        PeriodicTask.objects.filter(name=task_name).delete()

    def _unregister_polling(self, workflow_id: int):
        """
        Remove all polling tasks for this workflow.
        """
        from django_celery_beat.models import PeriodicTask
        PeriodicTask.objects.filter(name__startswith=f"workflow-polling-{workflow_id}-").delete()


_trigger_manager = None

def get_trigger_manager():
    global _trigger_manager
    if _trigger_manager is None:
        _trigger_manager = TriggerManager()
    return _trigger_manager
