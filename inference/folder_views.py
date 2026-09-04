"""
The folder tree and the recycle bin over HTTP.

A second views module in a flat app, following `extraction_views.py`. Every
route here is owner-scoped through exactly one function —
`filesystem.resolve_folder` — and every refusal it raises becomes a **404**,
never a 403: telling a caller "that exists but is not yours" is an ownership
oracle that lets anyone map which ids are real.

The API is **id-addressed, never path-addressed**. `path` and `folder_path` go
out for display; no route accepts either as a locator. That is the single
decision that keeps traversal off the table rather than guarded against.

Folder CRUD is sync DRF. The trash routes are `adrf` async only because
de-indexing crosses into the async engine — same shape as `document_detail`.
"""
from __future__ import annotations

import logging

from asgiref.sync import sync_to_async
from django.db.models import Count, Q
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from adrf.decorators import api_view as async_api_view
from core.http.pagination import paginate_keyset
from workflow_backend.thresholds import FOLDER_CHILDREN_LIMIT

from . import filesystem as fs
from . import recycle
from .models import Document, Folder
from .serializers import (
    DocumentListSerializer,
    FolderSerializer,
    FolderWriteSerializer,
    MoveSerializer,
    RestoreSerializer,
)

logger = logging.getLogger(__name__)


def _folder_error(exc) -> Response:
    """One place the two filesystem exceptions become status codes."""
    if isinstance(exc, fs.FolderNotFound):
        return Response({'error': str(exc)}, status=404)
    return Response({'error': str(exc)}, status=400)


def _with_counts(queryset):
    """Annotate child/document counts so a listing is not N+1.

    Both counts must exclude trashed rows. The annotation cannot inherit the
    default manager's filter — `Count` follows the raw relation — so the filter
    is spelled out here, the one place in the app where that is true.
    """
    return queryset.annotate(
        child_count_annotated=Count(
            'children', filter=Q(children__deleted_at__isnull=True), distinct=True),
        document_count_annotated=Count(
            'documents', filter=Q(documents__deleted_at__isnull=True), distinct=True),
    )


# ---------------------------------------------------------------------------
# Folders
# ---------------------------------------------------------------------------

@api_view(['GET', 'POST'])
@permission_classes([IsAuthenticated])
def folder_list(request):
    """
    GET: children of `?parent=<id>` (absent ⇒ the caller's root).
    POST: create a folder under `parent_id`.
    """
    if request.method == 'GET':
        try:
            parent = fs.resolve_folder(request.user, request.query_params.get('parent'))
        except fs.FolderNotFound as exc:
            return _folder_error(exc)

        children = _with_counts(
            Folder.objects.filter(user=request.user, parent=parent)
        ).order_by('name')
        # Capped rather than cursored: folder rows are tiny, and a second
        # pagination scheme on one page is worse than a cap. A capped response
        # says so in its own body — a truncated list and a complete one must
        # not look alike.
        page = list(children[:FOLDER_CHILDREN_LIMIT])
        return Response({
            'folder': FolderSerializer(parent).data if parent else None,
            'breadcrumbs': fs.breadcrumbs(parent),
            'folders': FolderSerializer(page, many=True).data,
            'count': len(page),
            'truncated': len(page) == FOLDER_CHILDREN_LIMIT,
        })

    body = FolderWriteSerializer(data=request.data)
    body.is_valid(raise_exception=True)
    try:
        parent = fs.resolve_folder(request.user, body.validated_data.get('parent_id'))
        folder = fs.create_folder(request.user, body.validated_data.get('name'), parent)
    except (fs.FolderNotFound, fs.FilesystemError) as exc:
        return _folder_error(exc)

    return Response(FolderSerializer(folder).data, status=201)


@async_api_view(['GET', 'PATCH', 'DELETE'])
@permission_classes([IsAuthenticated])
async def folder_detail(request, folder_id: int):
    """GET one folder; PATCH renames and/or moves it; DELETE sends it to the bin.

    Async for the same reason `document_detail` is: DELETE crosses into the
    engine to drop vectors. Keeping all three verbs on one route matters more
    than the sync/async split — a `/delete/` sub-path would be a wart the
    client has to remember.
    """
    try:
        folder = await sync_to_async(fs.resolve_folder)(request.user, folder_id)
    except fs.FolderNotFound as exc:
        return _folder_error(exc)

    if request.method == 'DELETE':
        result = await sync_to_async(recycle.trash)(request.user, folders=[folder])
        return Response(result, status=200)

    if request.method == 'PATCH':
        body = FolderWriteSerializer(data=request.data, partial=True)
        await sync_to_async(body.is_valid)(raise_exception=True)
        data = body.validated_data

        def _apply():
            f = folder
            if 'name' in data:
                f = fs.rename_folder(f, data['name'])
            if 'parent_id' in data:
                target = fs.resolve_folder(request.user, data['parent_id'])
                fs.move(request.user, folders=[f], target=target)
                f.refresh_from_db()
            return f

        try:
            folder = await sync_to_async(_apply)()
        except (fs.FolderNotFound, fs.FilesystemError) as exc:
            return _folder_error(exc)

    def _render():
        return {
            **FolderSerializer(folder).data,
            'breadcrumbs': fs.breadcrumbs(folder),
        }

    return Response(await sync_to_async(_render)())


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def fs_move(request):
    """Reparent folders and/or documents in one call.

    Bulk because the client needs multi-select drag; one route per item would
    be N requests and N chances to half-apply a move the user thinks is atomic.
    """
    body = MoveSerializer(data=request.data)
    body.is_valid(raise_exception=True)
    data = body.validated_data

    try:
        target = fs.resolve_folder(request.user, data.get('target_folder_id'))
        folders = fs.resolve_folders(request.user, data.get('folder_ids'))
        documents = fs.resolve_documents(request.user, data.get('document_ids'))
        result = fs.move(request.user, folders=folders, documents=documents, target=target)
    except (fs.FolderNotFound, fs.FilesystemError) as exc:
        return _folder_error(exc)

    return Response(result)


# ---------------------------------------------------------------------------
# Recycle bin
# ---------------------------------------------------------------------------

@api_view(['GET'])
@permission_classes([IsAuthenticated])
def trash_list(request):
    """What the caller can still restore.

    Only rows the user deleted *directly* — a folder's descendants carry
    `deleted_at` too, and listing them would turn one delete into 300 entries.
    """
    folders = list(
        Folder.all_objects.filter(
            user=request.user, deleted_at__isnull=False, trashed_directly=True,
        ).order_by('-deleted_at')[:FOLDER_CHILDREN_LIMIT]
    )
    documents = Document.all_objects.filter(
        user=request.user, deleted_at__isnull=False, trashed_directly=True,
    ).select_related('user', 'knowledge_base').order_by('-created_at')

    page = paginate_keyset(
        documents, limit=50, cursor=request.query_params.get('cursor'),
    )

    return Response({
        'folders': [
            {
                **FolderSerializer(f).data,
                'deleted_at': f.deleted_at,
                'purges_at': recycle.purges_at(f.deleted_at),
            }
            for f in folders
        ],
        'folders_truncated': len(folders) == FOLDER_CHILDREN_LIMIT,
        'documents': [
            {
                **DocumentListSerializer(d).data,
                'deleted_at': d.deleted_at,
                'purges_at': recycle.purges_at(d.deleted_at),
            }
            for d in page.items
        ],
        'next_cursor': page.next_cursor,
        'has_more': page.has_more,
        # Reported, never hardcoded by a client — it is an env var.
        'purges_after_days': recycle.retention_days(),
    })


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def trash_restore(request):
    """Put trashed rows back where they came from.

    There is deliberately no target parameter — see `recycle.restore`. Answers
    per-item outcomes rather than a bare boolean, because restoring 40 items
    where 3 had a purged parent has to be reportable.
    """
    body = RestoreSerializer(data=request.data)
    body.is_valid(raise_exception=True)
    data = body.validated_data

    result = recycle.restore(
        request.user,
        folder_ids=data.get('folder_ids'),
        document_ids=data.get('document_ids'),
    )
    return Response(result)


@api_view(['DELETE'])
@permission_classes([IsAuthenticated])
def trash_empty(request):
    """Purge the caller's bin now — the sweep with retention 0, scoped."""
    return Response(recycle.empty_bin(request.user))
