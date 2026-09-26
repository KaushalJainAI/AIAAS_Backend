"""
Recently opened files and saved app tabs over HTTP (inference/recents.py).

A views module in a flat app, following `folder_views.py`. Every rule lives in
`recents.py`; these views only translate. `RecentsError` becomes a 400 with
its message, and `NotFound` becomes a **404** for unknown and foreign ids
alike, so the endpoint cannot be used to learn which document ids exist.

Sync DRF: nothing here crosses into the async engine.
"""
from __future__ import annotations

from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from . import recents
from .serializers import DocumentListSerializer

_NOT_FOUND = {'error': 'File not found.'}


def _row(row) -> dict:
    return {
        'document': DocumentListSerializer(row.document).data,
        'app': row.app,
        'opened_at': row.opened_at,
        'open_count': row.open_count,
        'view_state': row.view_state or {},
    }


def _int_param(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


@api_view(['GET', 'POST', 'DELETE'])
@permission_classes([IsAuthenticated])
def recent_list(request):
    """GET the recents, POST an open, DELETE to clear them all."""
    try:
        if request.method == 'GET':
            types = [t for t in (request.query_params.get('types') or '').split(',') if t]
            rows = recents.listing(
                request.user,
                app=request.query_params.get('app', ''),
                types=types or None,
                limit=_int_param(request.query_params.get('limit'), 20),
            )
            return Response({'results': [_row(r) for r in rows]})
        if request.method == 'DELETE':
            return Response({'removed': recents.forget(request.user)})
        data = request.data if isinstance(request.data, dict) else {}
        row = recents.record_open(
            request.user,
            data.get('document_id'),
            app=data.get('app', ''),
            view_state=data.get('view_state'),
        )
        return Response(_row(row), status=201)
    except recents.RecentsError as exc:
        return Response({'error': str(exc)}, status=400)
    except recents.NotFound:
        return Response(_NOT_FOUND, status=404)


@api_view(['GET', 'PATCH', 'DELETE'])
@permission_classes([IsAuthenticated])
def recent_detail(request, document_id: int):
    """GET or PATCH where the user is in a file, or DELETE it from the recents."""
    try:
        if request.method == 'GET':
            return Response({'view_state': recents.get_view_state(request.user, document_id)})
        if request.method == 'DELETE':
            return Response({'removed': recents.forget(request.user, document_id)})
        data = request.data if isinstance(request.data, dict) else {}
        row = recents.save_view_state(request.user, document_id, data.get('view_state'))
        return Response({'view_state': row.view_state})
    except recents.RecentsError as exc:
        return Response({'error': str(exc)}, status=400)
    except recents.NotFound:
        return Response(_NOT_FOUND, status=404)


@api_view(['GET', 'PUT'])
@permission_classes([IsAuthenticated])
def app_session(request, app: str):
    """An app's open tabs: GET to restore, PUT to replace."""
    try:
        if request.method == 'GET':
            return Response(recents.session(request.user, app))
        data = request.data if isinstance(request.data, dict) else {}
        return Response(recents.save_session(request.user, app, data.get('tabs'), data.get('active')))
    except recents.RecentsError as exc:
        return Response({'error': str(exc)}, status=400)
