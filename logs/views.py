"""
`/api/logs/` — insights, execution history, and configuration history.

Views are thin on purpose: validate query params with a serializer, call one
function in `queries.py`, return the result. Every read is a plain synchronous
ORM call, so these are sync `@api_view`s (as in `agents/views/agents.py`) rather
than `async def` wrappers around a `sync_to_async` closure.

Note the vocabulary seam: the URL and query parameter are still spelled
`workflow_id`, and responses still carry `workflow_id` / `workflow_name`,
because the frontend and BrowserOS ship their own builds. What they identify is
a `SubAgent`. `queries.py` does the renaming in one place.
"""
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from . import queries
from .serializers import (
    AnalyticsFilterSerializer,
    BulkDeleteSerializer,
    ExecutionListFilterSerializer,
    RevisionListFilterSerializer,
)


def _validated(serializer_class, request) -> dict:
    serializer = serializer_class(data=request.query_params)
    serializer.is_valid(raise_exception=True)
    return serializer.validated_data


# ======================== Insights ========================

@extend_schema(responses={200: OpenApiTypes.OBJECT})
@api_view(['GET'])
@permission_classes([IsAuthenticated])
def execution_statistics(request):
    """Execution statistics for the authenticated user."""
    params = _validated(AnalyticsFilterSerializer, request)
    return Response(queries.execution_statistics(
        request.user, days=params['days'], agent_id=params.get('workflow_id')
    ))


@extend_schema(responses={200: OpenApiTypes.OBJECT})
@api_view(['GET'])
@permission_classes([IsAuthenticated])
def workflow_metrics(request, workflow_id: int):
    """Detailed metrics for one agent, including per-tool success rates."""
    metrics = queries.agent_metrics(request.user, workflow_id)
    if metrics is None:
        return Response({"error": "Agent not found"}, status=404)
    return Response(metrics)


@extend_schema(responses={200: OpenApiTypes.OBJECT})
@api_view(['GET'])
@permission_classes([IsAuthenticated])
def cost_breakdown(request):
    """Token and credit usage breakdown."""
    params = _validated(AnalyticsFilterSerializer, request)
    return Response(queries.cost_breakdown(request.user, days=params['days']))


@extend_schema(responses={200: OpenApiTypes.OBJECT})
@api_view(['GET'])
@permission_classes([IsAuthenticated])
def insights_overview(request):
    """One call for the Insights page: runs, spend, tools, delegation, quality."""
    params = _validated(AnalyticsFilterSerializer, request)
    from django.core.cache import cache

    days = params['days']
    compare = bool(params.get('compare'))
    key = f'insights:overview:{request.user.id}:{days}:{int(compare)}'
    try:
        cached = cache.get(key)
    except Exception:  # noqa: BLE001
        cached = None
    if cached is not None:
        return Response(cached)
    payload = queries.insights_overview(request.user, days=days, compare=compare)
    try:
        # 60s, same TTL as the witness/tool-overlay caches: there is no
        # invalidation hook here, so the TTL is the whole bound. A cache
        # failure computes live rather than returning empty.
        cache.set(key, payload, 60)
    except Exception:  # noqa: BLE001
        pass
    return Response(payload)


# ======================== Execution history ========================

@extend_schema(responses={200: OpenApiTypes.OBJECT})
@api_view(['GET'])
@permission_classes([IsAuthenticated])
def execution_list(request):
    """One page of the user's runs, newest first."""
    params = _validated(ExecutionListFilterSerializer, request)
    return Response(queries.execution_page(
        request.user,
        limit=params['limit'],
        cursor=params.get('cursor'),
        agent_id=params.get('workflow_id'),
        status=params.get('status'),
        caller=params.get('caller'),
        failure_category=params.get('failure_category') or None,
    ))


@extend_schema(responses={200: OpenApiTypes.OBJECT})
@api_view(['GET', 'DELETE'])
@permission_classes([IsAuthenticated])
def execution_detail(request, execution_id: str):
    """One run as turns, each with the reasoning behind it and its tool calls.

    Also carries the revision the run executed under, and — for a delegated run
    — who asked for it and what they were thinking.

    DELETE removes the run and its turns/steps/trace. Live runs are refused
    (stop first) and eval-caller runs are refused (delete the sweep instead);
    cost rows are kept so spend totals don't silently drop.
    """
    if request.method == 'DELETE':
        return _delete_execution(request, execution_id)
    detail = queries.execution_detail(request.user, execution_id)
    if detail is None:
        return Response({"error": "Execution not found"}, status=404)
    return Response(detail)


#: Refusing deletion while the run can still change. `timeout` is terminal.
LIVE_DELETE_STATUSES = {'pending', 'running', 'paused'}


def _delete_execution(request, execution_id: str):
    """Delete one finished run owned by the caller. 404/409, never 403."""
    from django.core.exceptions import ValidationError as DjangoValidationError
    from django.db import transaction

    from .models import ExecutionLog

    try:
        log = ExecutionLog.objects.filter(
            execution_id=execution_id, user=request.user,
        ).first()
    except (DjangoValidationError, ValueError, TypeError):
        # Malformed UUID, like the GET path: a 404, not a 500.
        return Response({"error": "Execution not found"}, status=404)
    if log is None:
        return Response({"error": "Execution not found"}, status=404)
    if log.status in LIVE_DELETE_STATUSES:
        return Response(
            {"error": "Stop the run first — a live run cannot be deleted."},
            status=409,
        )
    if log.caller == 'eval':
        return Response(
            {"error": "Delete the eval sweep instead — it owns this run's trace."},
            status=409,
        )
    with transaction.atomic():
        _release_run_resources(log)
        log.delete()
    return Response(status=204)


def _release_run_resources(log) -> None:
    """Best-effort: a deleted run must not hold file leases or read sets."""
    try:
        from workspaces.leases import release_holder

        release_holder(log.id)
    except Exception:  # noqa: BLE001
        pass
    try:
        from workspaces import reads as _reads

        _reads.discard_thread((log.input_data or {}).get('thread_id') or '')
    except Exception:  # noqa: BLE001
        pass


@extend_schema(responses={200: OpenApiTypes.OBJECT})
@api_view(['POST'])
@permission_classes([IsAuthenticated])
def execution_bulk_delete(request):
    """Delete many finished runs at once: `{ids}` or `{status, older_than_days}`.

    Refuses the whole request (nothing deleted) when any selected row is live
    or eval-owned. Cost rows are kept. Atomic: all or nothing.
    """
    from django.db import transaction

    from .models import ExecutionLog

    form = BulkDeleteSerializer(data=request.data or {})
    form.is_valid(raise_exception=True)
    data = form.validated_data

    if data.get('ids'):
        qs = ExecutionLog.objects.filter(
            user=request.user,
            execution_id__in=[str(i) for i in data['ids']],
        )
        if qs.count() != len(data['ids']):
            # Unknown or foreign id — 404 for both, so neither enumerates.
            return Response({"error": "One or more runs were not found."}, status=404)
    else:
        from datetime import timedelta

        from django.utils import timezone

        qs = ExecutionLog.objects.filter(user=request.user, status=data['status'])
        if data.get('older_than_days'):
            cutoff = timezone.now() - timedelta(days=data['older_than_days'])
            qs = qs.filter(completed_at__lt=cutoff)

    rows = list(qs)
    live = [r for r in rows if r.status in LIVE_DELETE_STATUSES]
    if live:
        return Response(
            {"error": f"{len(live)} selected run(s) are still live — stop them first."},
            status=409,
        )
    owned_eval = [r for r in rows if r.caller == 'eval']
    if owned_eval:
        return Response(
            {"error": "Delete eval sweeps instead — they own their runs' traces."},
            status=409,
        )
    with transaction.atomic():
        for log in rows:
            _release_run_resources(log)
            log.delete()
    return Response({"deleted": len(rows)})


@extend_schema(responses={200: OpenApiTypes.OBJECT})
@api_view(['POST'])
@permission_classes([IsAuthenticated])
def execution_mark_failed(request, execution_id: str):
    """Close one run stuck `running` as failed, without waiting for the sweep.

    Runs the same close path the recovery sweep would (`recovery._fail` for
    that one row), so leases release and the failure category is set. Anything
    not `running` is a 409 — this is for stuck runs, not a status editor.
    """
    from asgiref.sync import async_to_sync
    from django.core.exceptions import ValidationError as DjangoValidationError

    from agents.recovery import _fail

    from .models import ExecutionLog

    try:
        log = ExecutionLog.objects.filter(
            execution_id=execution_id, user=request.user,
        ).first()
    except (DjangoValidationError, ValueError, TypeError):
        return Response({"error": "Execution not found"}, status=404)
    if log is None:
        return Response({"error": "Execution not found"}, status=404)
    if log.status != 'running':
        return Response(
            {"error": f"This run is {log.status} — only a running run can be marked failed."},
            status=409,
        )
    async_to_sync(_fail)(log, 'Marked as failed by the user from the Activity page.')
    log.refresh_from_db()
    return Response({
        'execution_id': str(log.execution_id), 'status': log.status,
    })


# ======================== Judgement ========================

@extend_schema(responses={200: OpenApiTypes.OBJECT})
@api_view(['PUT', 'DELETE'])
@permission_classes([IsAuthenticated])
def feedback(request):
    """Thumbs up/down on a run or a chat message. Upsert on PUT, clear on DELETE."""
    from . import queries

    if request.method == 'DELETE':
        target = request.query_params.get('target', '')
        target_id = request.query_params.get('id', '')
        if queries.clear_feedback(request.user, target=target, target_id=target_id):
            return Response({'cleared': True})
        return Response({'error': 'Feedback not found'}, status=404)
    body = request.data or {}
    result = queries.upsert_feedback(
        request.user,
        target=body.get('target', ''), target_id=body.get('id'),
        rating=body.get('rating'), reason=body.get('reason', ''),
        comment=body.get('comment', ''),
    )
    if result is None:
        return Response({'error': 'Target not found'}, status=404)
    if 'error' in result:
        return Response(result, status=400)
    return Response({'feedback': result})


@extend_schema(responses={200: OpenApiTypes.OBJECT})
@api_view(['GET'])
@permission_classes([IsAuthenticated])
def quality(request):
    """Quality counts: failures by category, thumbs, signals. Excludes eval."""
    from . import queries
    from .serializers import AnalyticsFilterSerializer as _F

    serializer = _F(data=request.query_params)
    serializer.is_valid(raise_exception=True)
    return Response(queries.quality_summary(
        request.user, days=serializer.validated_data['days']))


# ======================== Configuration history ========================

@extend_schema(responses={200: OpenApiTypes.OBJECT})
@api_view(['GET'])
@permission_classes([IsAuthenticated])
def revision_list(request, agent_id: int):
    """One page of an agent's configuration changes, newest first, with diffs.

    Paged rather than capped: the history grows for the life of the agent, so
    the builder shows only the newest few and the full timeline has its own
    page, which walks the rest with `cursor`.
    """
    params = _validated(RevisionListFilterSerializer, request)
    timeline = queries.revision_timeline(
        request.user, agent_id, limit=params['limit'], cursor=params.get('cursor')
    )
    if timeline is None:
        return Response({"error": "Agent not found"}, status=404)
    return Response(timeline)


@extend_schema(responses={200: OpenApiTypes.OBJECT})
@api_view(['GET'])
@permission_classes([IsAuthenticated])
def revision_detail(request, agent_id: int, number: int):
    """One revision's full configuration snapshot."""
    revision = queries.revision_detail(request.user, agent_id, number)
    if revision is None:
        return Response({"error": "Revision not found"}, status=404)
    return Response(revision)
