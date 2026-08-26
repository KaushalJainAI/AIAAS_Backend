"""
Celery entry point for the HITL reminder sweep.

The task is a thin wrapper: all behaviour lives in `reminders.run_reminder_sweep`
so that `manage.py send_hitl_reminders` does exactly the same thing without a
broker. Local dev runs with RUN_WORKFLOWS_ASYNC=False and no Redis, and a
beat-only design would silently never fire there.
"""

import logging

from celery import shared_task

logger = logging.getLogger(__name__)


@shared_task(name='notifications.sweep_hitl_reminders', ignore_result=True)
def sweep_hitl_reminders():
    """Fire every HITL nudge that is due. Scheduled by Celery beat."""
    from .reminders import run_reminder_sweep

    try:
        return run_reminder_sweep()
    except Exception as exc:
        logger.exception("HITL reminder sweep failed: %s", exc)
        raise
