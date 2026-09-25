"""
Workspace job webhooks: a finished job wakes whoever is waiting on it.

On exit the workspace posts here with its secret; the view fires
`Trigger(mode='event', config={event: 'job.finished', job_id})` so a mission
(P7) or a waiting run resumes. No polling loop inside a run. The inbound
body is context, never the goal — the same webhook rule as messaging.
Every refusal is the same 404.

Two rules added 2026-09-25 (S4 in `docs/SECURITY_REVIEW_FIX_PLAN.md`):

- **Only the workspace owner's triggers fire, and only for this job.** It used
  to loop over every enabled `job.finished` trigger in the database, and an
  empty filter matched every job — so anyone holding their own workspace's
  secret could start any other user's agents, on that user's money.
- **Runs start detached** (`agents.scheduler.launch`, the scheduler's own
  path). `sweep.fire` blocked the request until every agent had finished.
"""
from __future__ import annotations

import logging

from adrf.decorators import api_view as async_api_view
from asgiref.sync import sync_to_async
from rest_framework.decorators import permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response

logger = logging.getLogger(__name__)


def _finish_job(secret: str, data) -> tuple | None:
    """Close the job; return (job, owner's matching triggers), or None."""
    from django.utils import timezone

    from agents.models import Trigger
    from workspaces.models import Workspace, WorkspaceJob

    if not secret:  # an unset secret is '' on every row; never a credential
        return None
    ws = Workspace.objects.filter(secret=secret).first()
    if ws is None:
        return None
    job = WorkspaceJob.objects.filter(id=data.get('job_id'), workspace=ws).first()
    if job is None:
        return None
    job.status = 'done' if data.get('ok', True) else 'failed'
    job.exit_code = data.get('exit_code')
    job.ended_at = timezone.now()
    job.save(update_fields=['status', 'exit_code', 'ended_at'])

    # An event trigger keeps its event in `config` ({"event": ..., "job_id":
    # ...}); there is no `event` or `filter` column. The old query named both,
    # raised FieldError on every call, and a bare `except: pass` hid it.
    triggers = [
        t for t in Trigger.objects.select_related('subagent', 'subagent__user')
        .filter(mode='event', enabled=True, config__event='job.finished',
                subagent__user_id=ws.user_id)
        # A trigger waits on one job; one naming no job waits on none.
        if str((t.config or {}).get('job_id', '')) == str(job.id)
    ]
    return job, triggers


@async_api_view(['POST'])
@permission_classes([AllowAny])
async def job_finished(request, secret: str):
    from agents.scheduler import launch

    found = await sync_to_async(_finish_job)(secret, request.data)
    if found is None:
        return Response({'detail': 'Not found.'}, status=404)
    _job, triggers = found
    for trigger in triggers:
        try:
            await launch(trigger, trigger_type='webhook')
        except Exception:  # noqa: BLE001 — one waiter must not stop the rest
            logger.exception('[WorkspaceHook] trigger %s failed to launch', trigger.id)
    return Response({'ok': True})
