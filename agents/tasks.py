"""
Celery entry point for the trigger sweep.

A thin wrapper, exactly like `notifications/tasks.py`: all behaviour lives in
`sweep.run_trigger_sweep` so `manage.py run_due_triggers` does the same thing
without a broker. Local dev has no Redis, and a beat-only scheduler fails by
never firing — which looks identical to "nothing was due".
"""

import logging

from celery import shared_task

logger = logging.getLogger(__name__)


@shared_task(name='orchestrator.sweep_triggers', ignore_result=True)
def sweep_triggers():
    """Fire every schedule trigger that is due. Scheduled by Celery beat."""
    from .sweep import run_trigger_sweep

    try:
        return run_trigger_sweep()
    except Exception as exc:
        logger.exception('Trigger sweep failed: %s', exc)
        raise
