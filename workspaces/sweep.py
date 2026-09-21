"""
Idle workspaces hibernate; quota overruns are refused at exec time.

One entry point, reachable both as the Celery beat task
`workspaces.sweep_workspaces` and as `manage.py sweep_workspaces` — the usual
split, because local dev runs without Redis and a beat-only design would
silently never fire.
"""
from __future__ import annotations

import logging
from datetime import timedelta

from django.conf import settings
from django.utils import timezone

logger = logging.getLogger(__name__)


def _idle_seconds() -> int:
    try:
        return int(getattr(settings, 'WORKSPACE_IDLE_SECONDS', 900))
    except (TypeError, ValueError):
        return 900


def run_workspace_sweep(now=None) -> dict[str, int]:
    from workspaces.models import Workspace

    now = now or timezone.now()
    idle_before = now - timedelta(seconds=_idle_seconds())
    idle = Workspace.objects.filter(
        status='running', last_active_at__lt=idle_before,
    )
    count = 0
    for ws in idle:
        try:
            from workspaces import engine as _engine

            import asyncio

            asyncio.get_event_loop().run_until_complete(_engine.hibernate(ws))
        except Exception:  # noqa: BLE001
            logger.exception('[Workspaces] hibernate failed for %s', ws.id)
            continue
        ws.status = 'hibernated'
        ws.save(update_fields=['status'])
        count += 1
    return {'hibernated': count}
