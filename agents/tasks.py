"""
Celery entry points for the orchestrator sweeps.

Thin wrappers, exactly like `notifications/tasks.py`: all behaviour lives
beside the sweep (`sweep`, `recovery`, `chat.turn.prune`) so each is also
runnable as a management command without a broker. Local dev has no Redis,
and a beat-only scheduler fails by never firing — which looks identical to
"nothing was due".
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
        tally = async_to_sync(sweep_orphaned_runs)()
        # Eval sweeps die with the same restart; `agents` never imports `eval`
        # at module scope, so this stays a lazy import inside the function.
        from eval.recovery import sweep_orphaned_eval_runs

        eval_tally = async_to_sync(sweep_orphaned_eval_runs)()
        return {**tally, 'eval': eval_tally}
    except Exception as exc:
        logger.exception('Run recovery sweep failed: %s', exc)
        raise


@shared_task(name='orchestrator.prune_chat_checkpoints', ignore_result=True)
def prune_chat_checkpoints():
    """Delete old chat checkpoints beyond the latest few per thread.

    Scheduled by Celery beat on the recovery cadence, and runnable as
    `manage.py prune_chat_checkpoints` — see chat/turn/prune.py. No-op on
    anything but the Postgres saver.
    """
    from asgiref.sync import async_to_sync

    from chat.turn.prune import prune_chat_checkpoints as _prune

    try:
        return async_to_sync(_prune)()
    except Exception as exc:
        logger.exception('Chat checkpoint prune failed: %s', exc)
        raise
