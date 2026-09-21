"""
The mission sweep: start runs whose next_wake_at is due, wake waiting ones.

One entry point, reachable both as the Celery beat task
`missions.sweep_missions` and as `manage.py run_missions` — the usual split,
because local dev runs without Redis and a beat-only design would silently
never fire. Mission runs are unattended (`caller='mission'`), so they need
`allow_unattended`.
"""
from __future__ import annotations

import logging

from django.utils import timezone

logger = logging.getLogger(__name__)


def due_missions(now=None):
    from missions.models import Mission

    now = now or timezone.now()
    return (
        Mission.objects
        .filter(status__in=('active', 'waiting'))
        .filter(next_wake_at__isnull=False, next_wake_at__lte=now)
        .select_related('agent', 'agent__user')
    )


def run_mission_sweep(now=None) -> dict[str, int]:
    from asgiref.sync import async_to_sync

    from agents.agent.runtime import start_agent_run_and_wait

    now = now or timezone.now()
    counts: dict[str, int] = {}
    for mission in due_missions(now):
        if mission.deadline and now > mission.deadline:
            mission.status = 'paused'
            mission.save(update_fields=['status', 'updated_at'])
            counts['paused'] = counts.get('paused', 0) + 1
            continue
        goal = (mission.goal or '').strip()
        if not goal:
            counts['skipped'] = counts.get('skipped', 0) + 1
            continue
        try:
            async_to_sync(start_agent_run_and_wait)(
                mission.agent, goal, user=mission.user,
                trigger_type='api', caller='mission',
                mission_id=mission.id,
            )
            mission.runs_done = (mission.runs_done or 0) + 1
            mission.next_wake_at = None
            mission.save(update_fields=['runs_done', 'next_wake_at', 'updated_at'])
            counts['fired'] = counts.get('fired', 0) + 1
        except Exception as exc:  # noqa: BLE001
            logger.exception('[Missions] mission %s failed to start', mission.id)
            counts['failed'] = counts.get('failed', 0) + 1
    return counts
