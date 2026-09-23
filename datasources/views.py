"""
Private CRUD for the user's custom tools, plus sharing them.

Both viewsets answer only for the caller: `get_queryset()` filters on
`request.user`, so another user's row is a 404, never a 403 — a 403 for
"exists but not yours" would be an ownership oracle. `perform_create()`
stamps the caller as owner; the client never names one.

Sharing mirrors `agents/views/gallery.py`: a listing is a frozen snapshot
(`SharedTool.tool_config` + `auth_shape`), installing writes a private copy
owned by the installer, and credentials never travel — not as values, not
even as references.
"""
from __future__ import annotations

import logging

from django.db import transaction
from django.db.models import F, Q
from django.shortcuts import get_object_or_404
from django.utils.text import slugify
from rest_framework import status, viewsets
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from workflow_backend.thresholds import PUBLIC_CATALOGUE_LIMIT

from . import sharing
from .models import ApiConnection, DataConnection, SharedTool
from .serializers import ApiConnectionSerializer, DataConnectionSerializer

logger = logging.getLogger(__name__)


class _OwnedViewSet(viewsets.ModelViewSet):
    """`user=request.user` on every read and write. Subclasses name the model."""

    permission_classes = [IsAuthenticated]
    model = None  # type: ignore[assignment]
    serializer_class = None  # type: ignore[assignment]

    def get_queryset(self):
        return self.model.objects.filter(user=self.request.user).order_by('name')

    def perform_create(self, serializer):
        serializer.save(user=self.request.user)


class ApiConnectionViewSet(_OwnedViewSet):
    """The user's HTTP API tools. Auth is a vault reference, never a value."""

    model = ApiConnection
    serializer_class = ApiConnectionSerializer


class DataConnectionViewSet(_OwnedViewSet):
    """The user's database tools. Reads always; writes only with allow_write."""

    model = DataConnection
    serializer_class = DataConnectionSerializer


# ------------------------------------------------------------------ sharing
#
# The same contract as agent sharing (`agents/views/gallery.py`), one rung
# down: publish freezes a snapshot, install writes a private copy, withdraw
# unlists rather than deletes. `link` shares resolve by slug for any signed-in
# user; listings show `platform` + `public` plus the caller's own rows.


def _source_row(kind: str, tool_id: int, user):
    """The caller's own live row, or 404 (foreign ids included)."""
    if kind == 'api':
        return get_object_or_404(ApiConnection, id=tool_id, user=user)
    if kind == 'data':
        return get_object_or_404(DataConnection, id=tool_id, user=user)
    return None


def _find_share(kind: str, tool_id: int, user) -> SharedTool | None:
    field = 'api_connection_id' if kind == 'api' else 'data_connection_id'
    return (SharedTool.objects
            .filter(tool_kind=kind, author=user, **{field: tool_id})
            .first())


def _mint_tool_slug(name: str, exclude_pk: int | None = None) -> str:
    base = slugify(name)[:180] or 'tool'
    candidate = base
    counter = 1
    while True:
        clash = SharedTool.objects.filter(slug=candidate)
        if exclude_pk is not None:
            clash = clash.exclude(pk=exclude_pk)
        if not clash.exists():
            return candidate
        counter += 1
        candidate = f'{base}-{counter}'


def _present_share(share: SharedTool, *, viewer=None) -> dict:
    config = share.tool_config or {}
    operations = None
    if share.tool_kind == 'api':
        paths = (config.get('openapi_spec') or {}).get('paths') or {}
        if isinstance(paths, dict):
            operations = sum(
                1 for item in paths.values() if isinstance(item, dict))
    return {
        'slug': share.slug,
        'tool_kind': share.tool_kind,
        'name': share.name,
        'tagline': share.tagline,
        'description': share.description,
        'author': (share.author.get_full_name() or '').strip()
                  or share.author.username,
        'is_mine': viewer is not None and share.author_id == viewer.id,
        'visibility': share.visibility,
        'is_listed': share.is_listed,
        'install_count': share.install_count,
        'version': share.version,
        'updated_at': share.updated_at,
        # What the copy will need — a credential *type*, never a reference.
        'auth_shape': share.auth_shape or {},
        'operations_count': operations,
        # The frozen fields minus the (possibly large) spec, so the card
        # stays small; install reads the full snapshot server-side.
        'tool_config': {k: v for k, v in config.items()
                        if k != 'openapi_spec'},
    }


def _visible_share(slug: str, user) -> SharedTool | None:
    """One shared tool by slug, if this caller may see it.

    A `link` share resolves for anybody with the slug; a withdrawn one only
    for its author. Everything else is 404 — the same anti-oracle rule the
    private endpoints follow.
    """
    share = (SharedTool.objects.select_related('author')
             .filter(slug=slug).first())
    if share is None:
        return None
    if share.is_listed or share.author_id == user.id:
        return share
    return None


@api_view(['GET', 'POST', 'DELETE'])
@permission_classes([IsAuthenticated])
def tool_share(request, kind: str, tool_id: int):
    if kind not in ('api', 'data'):
        return Response({'error': f'Unknown tool kind "{kind}".'},
                        status=status.HTTP_404_NOT_FOUND)
    row = _source_row(kind, tool_id, request.user)
    if row is None:  # pragma: no cover — _source_row 404s first
        return Response({'error': 'No such tool.'},
                        status=status.HTTP_404_NOT_FOUND)
    share = _find_share(kind, tool_id, request.user)

    if request.method == 'DELETE':
        if share is None:
            return Response(status=status.HTTP_204_NO_CONTENT)
        share.is_listed = False
        share.save(update_fields=['is_listed', 'updated_at'])
        return Response(status=status.HTTP_204_NO_CONTENT)

    try:
        tool_config, auth_shape = sharing.tool_to_shareable(kind, row)
    except ValueError as exc:
        return Response({'error': str(exc)},
                        status=status.HTTP_400_BAD_REQUEST)

    if request.method == 'GET':
        # What publishing would send. Writes nothing.
        return Response({
            'published': share is not None,
            'slug': share.slug if share else None,
            'visibility': share.visibility if share else 'platform',
            'is_listed': share.is_listed if share else False,
            'version': share.version if share else 0,
            'install_count': share.install_count if share else 0,
            'tagline': share.tagline if share else row.name,
            'description': share.description if share else '',
            'auth_shape': auth_shape,
            'tool_config': tool_config,
        })

    tagline = (request.data.get('tagline') or '').strip()
    if not tagline:
        return Response(
            {'error': 'A one-line description is required.'},
            status=status.HTTP_400_BAD_REQUEST)
    visibility = request.data.get('visibility') or 'platform'
    if visibility not in dict(SharedTool.VISIBILITY_CHOICES):
        return Response({'error': f'Unknown visibility "{visibility}".'},
                        status=status.HTTP_400_BAD_REQUEST)

    fields = {
        'name': row.name,
        'tagline': tagline[:200],
        'description': (request.data.get('description') or '').strip(),
        'tool_config': tool_config,
        'auth_shape': auth_shape,
        'visibility': visibility,
        'is_listed': True,
    }
    source = {'api_connection': row} if kind == 'api' else {'data_connection': row}
    with transaction.atomic():
        if share is None:
            share = SharedTool.objects.create(
                tool_kind=kind, author=request.user,
                slug=_mint_tool_slug(row.name), **source, **fields)
        else:
            for key, value in fields.items():
                setattr(share, key, value)
            share.version = F('version') + 1
            share.save()
            share.refresh_from_db()

    logger.info('Tool %s/%s shared as %s by user %s',
                kind, row.id, share.slug, request.user.id)
    return Response(_present_share(share, viewer=request.user))


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def shared_tool_list(request):
    """Shared tools this user may see. Capped — function views get no DRF
    pagination, so the cap is this module's to set."""
    rows = list(
        SharedTool.objects
        .filter(Q(is_listed=True,
                  visibility__in=SharedTool.LISTED_VISIBILITIES)
                | Q(author=request.user))
        .select_related('author')
        .order_by('-install_count', '-updated_at')
        [:PUBLIC_CATALOGUE_LIMIT + 1])
    truncated = len(rows) > PUBLIC_CATALOGUE_LIMIT
    return Response({
        'results': [_present_share(s, viewer=request.user)
                    for s in rows[:PUBLIC_CATALOGUE_LIMIT]],
        'truncated': truncated,
    })


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def shared_tool_detail(request, slug: str):
    share = _visible_share(slug, request.user)
    if share is None:
        return Response({'error': 'No shared tool with this link.'},
                        status=status.HTTP_404_NOT_FOUND)
    return Response(_present_share(share, viewer=request.user))


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def shared_tool_install(request, slug: str):
    """Install a private copy. Auth starts empty; the response names the
    credential type to link (`credentials_needed`), if any."""
    share = _visible_share(slug, request.user)
    if share is None:
        return Response({'error': 'No shared tool with this link.'},
                        status=status.HTTP_404_NOT_FOUND)
    try:
        with transaction.atomic():
            row, needs = sharing.install_copy(
                share.tool_kind, share.tool_config, share.auth_shape,
                request.user)
            SharedTool.objects.filter(pk=share.pk).update(
                install_count=F('install_count') + 1)
    except Exception as exc:  # noqa: BLE001 — serializer errors carry detail
        detail = getattr(exc, 'detail', str(exc))
        return Response({'error': detail},
                        status=status.HTTP_400_BAD_REQUEST)
    if share.tool_kind == 'api':
        body = ApiConnectionSerializer(row).data
    else:
        body = DataConnectionSerializer(row).data
    credentials_needed = []
    if needs:
        credentials_needed.append({'tool': row.name, **needs})
    logger.info('Tool %s installed by user %s', slug, request.user.id)
    return Response({'tool': body, 'credentials_needed': credentials_needed},
                    status=status.HTTP_201_CREATED)
