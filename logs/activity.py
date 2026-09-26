"""
`/api/activity/` — everything running, and everything recently changed.

Two read-only endpoints for the Activity page. They live in `logs/` because it
is the lowest layer that can read these models — and every cross-app read below
is deferred into the function body. `Backend/.importlinter` forbids `logs`
*directly* importing `agents.agent`, `chat.turn`, `eval` or `missions`; a
top-level import of any of them would fail `lint-imports` and
`test_import_contracts.py`. Same-app reads (`logs.models`, `logs.queries`)
stay at the top.

- `GET live/` — one list of every live process of any kind for the caller:
  agent runs (including delegated workers), detached coding tasks, eval
  sweeps, generating eval worlds, live chat turns. Each item carries only the
  actions the backend actually supports, computed here so the UI never offers
  a verb that 409s. Capped, and says `truncated`.
- `GET recent-files/` — recent file and content activity: opened files,
  overwritten versions, workspace edits. File copy/move are synchronous column
  writes, so there is never a "copying…" live row — this feed is where they
  show up instead.

In-process caveat, stated on the live response: `ChatRun._runs` and the code
task registry are per-process memory, so on a multi-process deploy this
endpoint sees only its own process's chat turns and coding tasks. The
database-backed rows (runs, sweeps, worlds) are visible everywhere.
"""
from __future__ import annotations

import time
from typing import Any

from django.utils import timezone
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from logs.models import ExecutionLog

from . import queries

#: Nothing returns an unbounded list. Per-kind caps feed a global cap; the
#: response says `truncated` rather than looking complete.
LIVE_LIMIT = 100
FILES_LIMIT = 50

#: Live agent-run statuses. `timeout` is terminal, not live.
LIVE_RUN_STATUSES = ('running', 'pending', 'paused')


def _iso(value) -> str | None:
    return value.isoformat() if value is not None else None


def _elapsed_s(since) -> int | None:
    if since is None:
        return None
    return max(0, int((timezone.now() - since).total_seconds()))


def _agent_run_items(user) -> list[dict[str, Any]]:
    """Live runs, minus eval traffic (a sweep row covers those)."""
    from agents.budget import clamp_run_seconds
    from agents.recovery import ORPHAN_GRACE_SECONDS
    from agents.spend import rupees_for

    now = timezone.now()
    rows = list(
        ExecutionLog.objects.filter(user=user, status__in=LIVE_RUN_STATUSES)
        .exclude(caller='eval')
        .select_related('subagent', 'revision')
        .order_by('-created_at')[:LIVE_LIMIT]
    )
    items = []
    for log in rows:
        guards = (getattr(log.subagent, 'guardrails', None) or {}) if log.subagent_id else {}
        allowed = clamp_run_seconds(guards.get('maxRunSeconds'))
        started = log.started_at or log.created_at
        stuck = (
            log.status == 'running'
            and started is not None
            and (now - started).total_seconds() > allowed + ORPHAN_GRACE_SECONDS
        )
        actions = ['open']
        if log.status in ('running', 'paused'):
            actions.insert(0, 'stop')
        if stuck:
            actions.append('mark_failed')
        items.append({
            'kind': 'agent_run',
            'id': str(log.execution_id),
            'title': queries._agent_name(log) or 'Agent run',
            'status': log.status,
            'started_at': _iso(started),
            'elapsed_s': _elapsed_s(started),
            'spend_rupees': rupees_for(log.tokens_used),
            'caller': log.caller,
            'is_delegated': log.parent_step_id is not None,
            'mission_id': log.mission_id,
            'stuck': stuck,
            'actions': actions,
            'href': f'/runs?run={log.execution_id}',
        })
    return items


def _code_task_items(user) -> list[dict[str, Any]]:
    """Detached coding-task workers live in this process (see module note)."""
    from agents.agent import tasks as code_tasks

    records = [
        (parent, record)
        for parent, record in code_tasks.iter_live()
        if record.execution_id
    ]
    if not records:
        return []
    logs = {
        str(log.execution_id): log
        for log in ExecutionLog.objects.filter(
            user=user,
            execution_id__in=[r.execution_id for _, r in records],
        ).select_related('subagent')
    }
    items = []
    for _parent, record in records:
        log = logs.get(str(record.execution_id))
        if log is None:
            continue
        started_ms = record.started_at_ms or 0
        items.append({
            'kind': 'code_task',
            'id': str(record.execution_id),
            'title': record.title or record.label or 'Coding task',
            'status': record.status,
            'started_at': _iso(log.started_at or log.created_at),
            'elapsed_s': max(0, int(time.time() * 1000 - started_ms) // 1000) if started_ms else None,
            'spend_rupees': None,
            'agent': record.agent_name,
            'project': record.project_name,
            'actions': (
                ['stop', 'steer', 'open']
                if record.status in ('running', 'paused') else ['open']
            ),
            'href': f'/runs?run={record.execution_id}',
        })
    return items


def _eval_sweep_items(user) -> list[dict[str, Any]]:
    from eval.models import EvalRun

    rows = list(
        EvalRun.objects.filter(user=user, status__in=('pending', 'running'))
        .select_related('suite', 'subagent')
        .order_by('-created_at')[:50]
    )
    items = []
    for run in rows:
        suite_name = getattr(run.suite, 'name', None) or 'Suite'
        agent_name = getattr(run.subagent, 'name', None)
        title = suite_name if not agent_name else f'{suite_name} on {agent_name}'
        items.append({
            'kind': 'eval_sweep',
            'id': str(run.run_id),
            'title': title,
            'status': run.status,
            'started_at': _iso(run.started_at or run.created_at),
            'elapsed_s': _elapsed_s(run.started_at or run.created_at),
            'spend_rupees': None,
            'actions': ['stop', 'open'],
            'href': '/evals',
        })
    return items


def _eval_world_items(user) -> list[dict[str, Any]]:
    from eval.models import EvalWorld

    rows = list(
        EvalWorld.objects.filter(suite__user=user, status='generating')
        .select_related('suite')
        .order_by('-created_at')[:20]
    )
    items = []
    for world in rows:
        suite_name = getattr(world.suite, 'name', None) or 'Suite'
        items.append({
            'kind': 'eval_world',
            'id': str(world.id),
            # No cancel route exists for a world — open only, by design.
            'title': f'{suite_name} world v{world.version} (generating)',
            'status': world.status,
            'started_at': _iso(world.created_at),
            'elapsed_s': _elapsed_s(world.created_at),
            'spend_rupees': None,
            'actions': ['open'],
            'href': '/evals',
        })
    return items


def _chat_turn_items(user) -> list[dict[str, Any]]:
    from chat.models import ChatSession
    from chat.turn import runs as chat_runs

    keys = chat_runs.active_keys(user.id)
    if not keys:
        return []
    try:
        sessions = {
            str(row.id): row
            for row in ChatSession.objects.filter(user=user, id__in=keys)
        }
    except Exception:  # noqa: BLE001
        # A non-UUID key matches no session row — miss, not 500.
        return []
    now_mono = time.monotonic()
    items = []
    for key in keys:
        session = sessions.get(str(key))
        if session is None:
            continue
        run = chat_runs.get(key)
        elapsed = (
            max(0, int(now_mono - run.started_at))
            if run is not None else None
        )
        items.append({
            'kind': 'chat_turn',
            'id': str(key),
            'title': session.title or 'Chat',
            'status': 'running',
            'started_at': None,
            'elapsed_s': elapsed,
            'spend_rupees': None,
            'actions': ['stop', 'open'],
            'href': '/ai-chat',
        })
    return items


@extend_schema(responses={200: OpenApiTypes.OBJECT})
@api_view(['GET'])
@permission_classes([IsAuthenticated])
def activity_live(request):
    """Every live process of any kind for the caller, newest first."""
    items = (
        _agent_run_items(request.user)
        + _code_task_items(request.user)
        + _eval_sweep_items(request.user)
        + _eval_world_items(request.user)
        + _chat_turn_items(request.user)
    )
    # Wall-clock started sorts first, newest first; chat turns carry no wall
    # start (monotonic only), so they sort after dated rows, by elapsed.
    dated = [i for i in items if i['started_at']]
    undated = [i for i in items if not i['started_at']]
    dated.sort(key=lambda i: i['started_at'], reverse=True)
    undated.sort(key=lambda i: -(i['elapsed_s'] or 0))
    ordered = dated + undated
    truncated = len(ordered) > LIVE_LIMIT
    return Response({
        'items': ordered[:LIVE_LIMIT],
        'truncated': truncated,
        'note': (
            'Chat turns and coding tasks are tracked per server process; '
            'runs, sweeps and worlds are visible everywhere.'
        ),
    })


@extend_schema(responses={200: OpenApiTypes.OBJECT})
@api_view(['GET'])
@permission_classes([IsAuthenticated])
def activity_recent_files(request):
    """Recent file and content activity, newest first (capped, says so)."""
    from inference import recents
    from inference.models import DocumentVersion
    from workspaces.models import CodeChange

    feed: list[dict[str, Any]] = []
    try:
        for row in recents.listing(request.user, limit=20):
            doc = getattr(row, 'document', None)
            feed.append({
                'kind': 'file_opened',
                'at': _iso(getattr(row, 'opened_at', None)),
                'document_id': getattr(doc, 'id', None),
                'name': getattr(doc, 'name', None) or 'File',
                'app': getattr(row, 'app', '') or '',
                'open_count': getattr(row, 'open_count', 1),
                'href': f'/documents?doc={doc.id}' if doc is not None else '/apps',
            })
    except Exception:  # noqa: BLE001 — one feed failing must not fail the page
        pass
    for version in (
        DocumentVersion.objects.filter(document__user=request.user)
        .select_related('document')
        .order_by('-created_at')[:20]
    ):
        feed.append({
            'kind': 'file_version',
            'at': _iso(version.created_at),
            'document_id': version.document_id,
            'name': version.name or getattr(version.document, 'name', 'File'),
            'source': version.source,
            'href': f'/documents?doc={version.document_id}',
        })
    for change in (
        CodeChange.objects.filter(run__user=request.user)
        .select_related('run')
        .order_by('-created_at')[:20]
    ):
        execution_id = getattr(change.run, 'execution_id', None)
        feed.append({
            'kind': 'code_change',
            'at': _iso(change.created_at),
            'path': change.path,
            'name': change.path,
            'href': f'/runs?run={execution_id}' if execution_id else '/runs',
        })
    feed.sort(key=lambda i: i['at'] or '', reverse=True)
    truncated = len(feed) > FILES_LIMIT
    return Response({'items': feed[:FILES_LIMIT], 'truncated': truncated})
