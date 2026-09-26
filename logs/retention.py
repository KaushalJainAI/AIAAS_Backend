"""
How long a run's inner detail is kept: storage limitation, applied.

Every agent turn stores the model's full reasoning (`AgentTurn.reasoning`,
`content`) and every tool call its arguments and result (`AgentStep.args`,
`result`) — mail bodies read, search results, file contents, the user's own
data passing through. That record is what makes a run debuggable and
auditable, and nothing ever removed it. India's DPDP Act (s.8(7)) and the GDPR
both require personal data to be kept no longer than its purpose needs, while
the EU AI Act asks deployers of high-risk systems to keep logs for at least six
months.

So the detail ages out and the record does not. After
`RUN_DETAIL_RETENTION_DAYS` (default 180, the six-month floor) the reasoning
and the tool payloads are cleared; the run row, its status, answer, cost,
timing, tool names, order and approvals stay, so history, spend reporting and
"which tools did it call and who approved them" keep working. Only finished
runs age: a paused run may still be resumed and needs its detail.

Runs daily in the in-process scheduler (`agents/scheduler.py::PERIODIC_JOBS`,
which is what production uses — it runs no Celery), and is also reachable as
the beat task `logs.redact_old_run_detail` and as `manage.py purge_run_detail`,
the same split as the recycle-bin purge.
"""
from __future__ import annotations

import logging
from datetime import timedelta

from django.conf import settings
from django.utils import timezone

logger = logging.getLogger(__name__)

DEFAULT_RETENTION_DAYS = 180
_FINISHED = ('completed', 'failed', 'cancelled', 'timeout')
REDACTED = {'redacted': 'retention'}


def retention_days(days: int | None = None) -> int:
    if days is not None:
        return max(0, int(days))
    try:
        return max(0, int(getattr(settings, 'RUN_DETAIL_RETENTION_DAYS', DEFAULT_RETENTION_DAYS)))
    except (TypeError, ValueError):
        return DEFAULT_RETENTION_DAYS


def _due(days: int | None):
    from .models import AgentStep, AgentTurn

    cutoff = timezone.now() - timedelta(days=retention_days(days))
    runs = dict(execution__status__in=_FINISHED, execution__started_at__lt=cutoff)
    turns = AgentTurn.objects.filter(**runs).exclude(reasoning='', content='')
    steps = AgentStep.objects.filter(**runs).exclude(args=REDACTED)
    return cutoff, turns, steps


def pending_counts(days: int | None = None) -> dict:
    cutoff, turns, steps = _due(days)
    return {'cutoff': cutoff, 'turns': turns.count(), 'steps': steps.count()}


def run_retention_sweep(days: int | None = None) -> dict:
    """Clear reasoning and tool payloads from finished runs past retention."""
    cutoff, turns, steps = _due(days)
    cleared_turns = turns.update(reasoning='', content='')
    cleared_steps = steps.update(args=REDACTED, result=REDACTED)
    if cleared_turns or cleared_steps:
        logger.info('[Retention] Cleared detail from %s turns and %s steps before %s',
                    cleared_turns, cleared_steps, cutoff)
    return {'cutoff': cutoff, 'turns': cleared_turns, 'steps': cleared_steps}
