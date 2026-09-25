"""
Sweeps that outlived their process: closing what a restart dropped.

Mirrors `agents/recovery.py` for the same reason it exists: a sweep is a
detached task (`background.spawn`), so when the process holding it goes away
the `EvalRun` stays `running` for ever and its `running`/`pending` results
with it. Unlike agent runs, a sweep is never resumed — the grading half is
dead with the process, so resuming would spend money for nobody.

An orphan is a `running` run whose `updated_at` is older than
`EVAL_ORPHAN_SECONDS`. `updated_at` works because every `_save_result` touches
it (see `runner._save_result`), so a long but live sweep is never mistaken
for dead.
"""
from __future__ import annotations

import logging
from datetime import timedelta

from asgiref.sync import sync_to_async
from django.utils import timezone

from workflow_backend.thresholds import EVAL_ORPHAN_SECONDS

logger = logging.getLogger(__name__)

ORPHAN_MESSAGE = 'interrupted — the process running this sweep stopped'


@sync_to_async
def _orphans(limit: int = 20) -> list:
    from .models import EvalRun

    cutoff = timezone.now() - timedelta(seconds=EVAL_ORPHAN_SECONDS)
    return list(
        EvalRun.objects.filter(status='running', updated_at__lt=cutoff)
        .order_by('updated_at')[:limit]
    )


@sync_to_async
def _close(run) -> bool:
    """Fail a stale run and error its open results. Returns True if closed."""
    from . import supervision
    from .models import EvalResult, EvalRun

    fresh = EvalRun.objects.filter(pk=run.pk, status='running').first()
    if fresh is None:
        return False
    EvalResult.objects.filter(
        run_id=fresh.pk, status__in=('running', 'pending')
    ).update(
        status='error', error_message=ORPHAN_MESSAGE,
        updated_at=timezone.now(),
    )
    fresh.status = 'failed'
    fresh.error_message = ORPHAN_MESSAGE
    fresh.completed_at = fresh.completed_at or timezone.now()
    if fresh.started_at and fresh.completed_at:
        fresh.duration_ms = int(
            (fresh.completed_at - fresh.started_at).total_seconds() * 1000
        )
    fresh.save(update_fields=[
        'status', 'error_message', 'completed_at', 'duration_ms', 'updated_at',
    ])
    supervision.recompute(fresh)
    return True


async def sweep_orphaned_eval_runs(limit: int = 20) -> dict:
    """Close every sweep whose process is gone. Returns a tally."""
    tally = {'checked': 0, 'closed': 0}
    for run in await _orphans(limit):
        tally['checked'] += 1
        try:
            if await _close(run):
                tally['closed'] += 1
                logger.warning('[EvalRecovery] Closed orphaned sweep %s', run.run_id)
        except Exception:  # noqa: BLE001
            logger.exception('[EvalRecovery] Could not close sweep %s', run.run_id)
    # World generations run detached too, and die with their process the same
    # way: a row left `generating` for ever would also block every retry.
    try:
        from asgiref.sync import sync_to_async

        from .api import fail_stale_world_generations

        tally['worlds_failed'] = await sync_to_async(fail_stale_world_generations)()
    except Exception:  # noqa: BLE001
        logger.exception('[EvalRecovery] Could not close stale world generations')
    return tally


__all__ = ['ORPHAN_MESSAGE', 'sweep_orphaned_eval_runs']
