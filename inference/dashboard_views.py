"""
Dashboards: live tiles bound to sources, readable and refreshed in-browser.

A dashboard is a `Dashboard` row (title, validated spec, sources,
refresh_cron, visibility). Same visibility ladder as published pages
(`link < platform < public`, default `platform`); every refusal of a foreign
row is the same 404 so listings cannot oracle private work. Id-addressed:
no slug, no path locator.

`refresh` re-runs nothing yet — sources execute in a later phase — so it
returns the stored spec with `refreshed_at`. The route exists now so the
upcoming `/dashboards` page has a stable refresh door to call.
"""
from __future__ import annotations

from django.utils import timezone
from drf_spectacular.utils import OpenApiResponse, extend_schema
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from workflow_backend.thresholds import PUBLISHED_PAGE_LIST_LIMIT

from .models import Dashboard

LIST_CAP = PUBLISHED_PAGE_LIST_LIMIT
VISIBILITIES = ('link', 'platform', 'public')


def _present(d: Dashboard) -> dict:
    return {
        'id': d.id,
        'title': d.title,
        'spec': d.spec,
        'sources': d.sources,
        'refresh_cron': d.refresh_cron,
        'visibility': d.visibility,
        'updated_at': d.updated_at,
        'created_at': d.created_at,
    }


def _validate_payload(data: dict, *, partial: bool = False) -> dict:
    """Validate create/update body through the same rules as the tool."""
    from chat.tools.dashboards import _validate_spec

    out: dict = {}
    if not partial or 'title' in data or 'tiles' in data:
        spec = _validate_spec({
            'title': data.get('title', ''),
            'tiles': data.get('tiles', []),
        })
        out['title'] = spec['title']
        out['spec'] = spec
    if 'sources' in data:
        sources = data.get('sources') or []
        if not isinstance(sources, list):
            raise ValueError('sources must be a list.')
        out['sources'] = sources[:20]
    if 'refresh_cron' in data:
        out['refresh_cron'] = str(data.get('refresh_cron') or '')[:100]
    if 'visibility' in data:
        visibility = str(data.get('visibility') or 'platform')
        if visibility not in VISIBILITIES:
            raise ValueError(f"visibility must be one of {', '.join(VISIBILITIES)}.")
        out['visibility'] = visibility
    return out


@extend_schema(methods=['GET'], responses={200: OpenApiResponse(description='Dashboards.')})
@api_view(['GET', 'POST'])
@permission_classes([IsAuthenticated])
def dashboard_list(request):
    if request.method == 'GET':
        if request.query_params.get('scope') == 'platform':
            source = Dashboard.objects.filter(visibility__in=('platform', 'public'))
        else:
            source = Dashboard.objects.filter(user=request.user)
        rows = list(source.order_by('-updated_at')[:LIST_CAP + 1])
        truncated = len(rows) > LIST_CAP
        return Response({
            'results': [_present(d) for d in rows[:LIST_CAP]],
            'truncated': truncated,
        })

    try:
        fields = _validate_payload(request.data)
    except ValueError as exc:
        return Response({'error': str(exc)}, status=status.HTTP_400_BAD_REQUEST)
    fields.setdefault('visibility', 'platform')
    d = Dashboard.objects.create(user=request.user, **fields)
    return Response(_present(d), status=status.HTTP_201_CREATED)


@extend_schema(methods=['GET'], responses={200: OpenApiResponse(description='One dashboard.')})
@api_view(['GET', 'PATCH', 'DELETE'])
@permission_classes([IsAuthenticated])
def dashboard_detail(request, dashboard_id: int):
    d = Dashboard.objects.filter(id=dashboard_id).first()
    if d is None or (d.user_id != request.user.id and d.visibility not in ('platform', 'public')):
        return Response({'error': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)

    if request.method == 'GET':
        return Response(_present(d))

    if d.user_id != request.user.id:
        return Response({'error': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)

    if request.method == 'DELETE':
        d.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)

    try:
        fields = _validate_payload(request.data, partial=True)
    except ValueError as exc:
        return Response({'error': str(exc)}, status=status.HTTP_400_BAD_REQUEST)
    for key, value in fields.items():
        setattr(d, key, value)
    d.save()
    return Response(_present(d))


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def dashboard_refresh(request, dashboard_id: int):
    d = Dashboard.objects.filter(id=dashboard_id).first()
    if d is None or (d.user_id != request.user.id and d.visibility not in ('platform', 'public')):
        return Response({'error': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)
    # Sources do not execute yet (later phase); the door is stable so the
    # `/dashboards` page can call it from day one.
    return Response({**_present(d), 'refreshed_at': timezone.now()})
