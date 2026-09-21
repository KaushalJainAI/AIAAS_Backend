"""
Workspace job webhooks: a finished job wakes whoever is waiting on it.

On exit the workspace posts here with its secret; the view fires
`Trigger(mode='event', event='job.finished', filter={job_id})` so a mission
(P7) or a waiting run resumes. No polling loop inside a run. The inbound
body is context, never the goal — the same webhook rule as messaging.
Every refusal is the same 404.
"""
from __future__ import annotations

from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response


@api_view(['POST'])
@permission_classes([AllowAny])
def job_finished(request, secret: str):
    from django.shortcuts import get_object_or_404

    from workspaces.models import Workspace

    ws = Workspace.objects.filter(secret=secret).first()
    if ws is None:
        return Response({'detail': 'Not found.'}, status=404)
    job_id = request.data.get('job_id')
    from workspaces.models import WorkspaceJob

    job = WorkspaceJob.objects.filter(id=job_id, workspace=ws).first()
    if job is None:
        return Response({'detail': 'Not found.'}, status=404)
    from django.utils import timezone

    job.status = 'done' if request.data.get('ok', True) else 'failed'
    job.exit_code = request.data.get('exit_code')
    job.ended_at = timezone.now()
    job.save(update_fields=['status', 'exit_code', 'ended_at'])
    try:
        from agents.models import Trigger

        for trigger in Trigger.objects.filter(
                mode='event', enabled=True, event='job.finished'):
            filt = trigger.filter or {}
            if filt and str(filt.get('job_id')) != str(job.id):
                continue
            from agents import sweep as _sweep

            _sweep.fire(trigger)
    except Exception:
        pass
    return Response({'ok': True})
