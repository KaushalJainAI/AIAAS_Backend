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


@shared_task(name='orchestrator.recover_runs', ignore_result=True)
def recover_runs():
    """Resume or close runs whose process went away. Scheduled by Celery beat.

    Same wrapper-only shape as the sweep above, and for a sharper version of
    the same reason: this is the recovery path for a process that died, so a
    design where it only runs under a broker would be unavailable in exactly
    the conditions that produce work for it.
    """
    from asgiref.sync import async_to_sync

    from .recovery import sweep_orphaned_runs

    try:
        return async_to_sync(sweep_orphaned_runs)()
    except Exception as exc:
        logger.exception('Run recovery sweep failed: %s', exc)
        raise
