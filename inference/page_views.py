"""
Hosted pages: snapshots of outputs shareable by link.

A **snapshot, not a pointer**, reusing the `SharedAgent` decisions rather than
inventing new ones: what a reader sees comes from frozen `body`/`file`,
never from a live document that could change under a link somebody already
holds. Withdrawing unlists rather than deletes.

Three visibilities, each strictly wider than the last, defaulting to the
middle: `link` (by slug, still needs an account) < `platform` (listed to
signed-in users) < `public` (readable with no account). The public pair below
is the app's third unauthenticated surface (after the webhook receiver and the
public agent catalogue) and inherits its rules: every refusal is the same 404,
the anonymous projection is a separate function, and the listing is capped
because DRF pagination never reaches `@api_view` function views.

**HTML safety**: `html`-kind bodies are never inlined on the app origin. The
API serves them as data; the frontend renders them inside a sandboxed iframe
(`sandbox` without `allow-same-origin`) with a strict CSP, so a published page
is never an XSS on our login cookies. Public responses also carry a CSP
header, pinned by the tests.
"""
from __future__ import annotations

import logging

from django.http import FileResponse
from django.utils import timezone
from drf_spectacular.utils import OpenApiResponse, extend_schema
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response

from workflow_backend.thresholds import PUBLISHED_PAGE_LIST_LIMIT

from .models import PublishedPage

logger = logging.getLogger(__name__)

#: Served on every public response. The API is JSON, so this is defence in
#: depth rather than the boundary itself — the boundary is that `html` bodies
#: are rendered client-side inside a sandboxed iframe, never inlined.
CSP_HEADER = (
    "default-src 'none'; frame-ancestors 'none'; base-uri 'none'; "
    "form-action 'none'"
)


# ------------------------------------------------------------------ presenting


def _present(page: PublishedPage, *, viewer=None) -> dict:
    return {
        'slug': page.slug,
        'title': page.title,
        'kind': page.kind,
        'body': page.body,
        'file_name': page.file_name,
        'has_file': bool(page.file),
        'visibility': page.visibility,
        'is_listed': page.is_listed,
        'is_mine': viewer is not None and page.owner_id == viewer.id,
        'updated_at': page.updated_at,
        'created_at': page.created_at,
    }


def _present_public(page: PublishedPage) -> dict:
    """One published page, as somebody with no account may see it.

    Its own function rather than `_present` with a flag: the signed-in shape
    carries `is_mine`, and a flag on one function is how account-shaped fields
    eventually leak into the anonymous response.
    """
    return {
        'slug': page.slug,
        'title': page.title,
        'kind': page.kind,
        'body': page.body,
        'file_name': page.file_name,
        'has_file': bool(page.file),
        'updated_at': page.updated_at,
    }


def _public_response(data: dict, *, status_code: int = 200) -> Response:
    response = Response(data, status=status_code)
    response['Content-Security-Policy'] = CSP_HEADER
    return response




def _visible_to(page: PublishedPage, user) -> bool:
    """Whether this signed-in caller may read this page."""
    if page.owner_id == user.id:
        return True
    if not page.is_listed:
        return False
    return page.visibility in ('platform', 'public', 'link')


# ---------------------------------------------------------------------- reads


@extend_schema(
    methods=['GET'],
    responses={200: OpenApiResponse(description='The caller\'s pages.')},
)
@api_view(['GET', 'POST'])
@permission_classes([IsAuthenticated])
def page_list(request):
    if request.method == 'GET':
        # `scope=platform`: what everyone on the platform has listed — the
        # meaning of `platform` visibility. Default: the caller's own pages.
        if request.query_params.get('scope') == 'platform':
            source = PublishedPage.objects.filter(
                is_listed=True, visibility__in=('platform', 'public'))
        else:
            source = PublishedPage.objects.filter(owner=request.user)
        pages = source.order_by('-updated_at')[:PUBLISHED_PAGE_LIST_LIMIT + 1]
        pages = list(pages)
        truncated = len(pages) > PUBLISHED_PAGE_LIST_LIMIT
        return Response({
            'results': [
                _present(p, viewer=request.user)
                for p in pages[:PUBLISHED_PAGE_LIST_LIMIT]
            ],
            'truncated': truncated,
        })

    from .pages import PublishError, publish

    try:
        page = publish(
            request.user,
            title=request.data.get('title') or '',
            kind=request.data.get('kind') or 'report',
            body=request.data.get('body') or '',
            visibility=request.data.get('visibility') or 'platform',
        )
    except PublishError as exc:
        return Response({'error': str(exc)}, status=status.HTTP_400_BAD_REQUEST)
    logger.info('Page %s published as %s by user %s',
                page.id, page.slug, request.user.id)
    return Response(_present(page, viewer=request.user),
                    status=status.HTTP_201_CREATED)


@extend_schema(
    methods=['GET'],
    responses={200: OpenApiResponse(description='One page.')},
)
@api_view(['GET', 'DELETE'])
@permission_classes([IsAuthenticated])
def page_detail(request, slug: str):
    page = PublishedPage.objects.filter(slug=slug).first()
    if page is None or not _visible_to(page, request.user):
        # A withdrawn page, a link page whose slug was never shared, and a
        # slug that never existed are indistinguishable without the account
        # that owns them — otherwise this is an oracle for enumerating what
        # people published privately.
        return Response({'error': 'Not found.'},
                        status=status.HTTP_404_NOT_FOUND)

    if request.method == 'DELETE':
        if page.owner_id != request.user.id:
            return Response({'error': 'Not found.'},
                            status=status.HTTP_404_NOT_FOUND)
        # Withdrawn, not deleted: links already handed out stop resolving
        # while nothing already read is rewritten, and relisting keeps the URL.
        page.is_listed = False
        page.withdrawn_at = timezone.now()
        page.save(update_fields=['is_listed', 'withdrawn_at', 'updated_at'])
        return Response(status=status.HTTP_204_NO_CONTENT)

    return Response(_present(page, viewer=request.user))


# --------------------------------------------------------------- public reads


@extend_schema(methods=['GET'], responses={200: OpenApiResponse(description='Public pages.')}, auth=[])
@api_view(['GET'])
@permission_classes([AllowAny])
def public_page_list(request):
    pages = list(
        PublishedPage.objects
        .filter(is_listed=True, visibility='public')
        .order_by('-updated_at')[:PUBLISHED_PAGE_LIST_LIMIT + 1]
    )
    truncated = len(pages) > PUBLISHED_PAGE_LIST_LIMIT
    return _public_response({
        'results': [_present_public(p) for p in pages[:PUBLISHED_PAGE_LIST_LIMIT]],
        'truncated': truncated,
    })


@extend_schema(methods=['GET'], responses={200: OpenApiResponse(description='One public page.')}, auth=[])
@api_view(['GET'])
@permission_classes([AllowAny])
def public_page_detail(request, slug: str):
    page = (
        PublishedPage.objects
        .filter(slug=slug, is_listed=True, visibility='public')
        .first()
    )
    if page is None:
        # Deliberately the same answer for "never existed", "not public",
        # "withdrawn" and "link-only".
        return _public_response({'error': 'Not found.'}, status_code=404)
    return _public_response(_present_public(page))


@extend_schema(methods=['GET'], responses={200: OpenApiResponse(description='The page file.')}, auth=[])
@api_view(['GET'])
@permission_classes([AllowAny])
def public_page_download(request, slug: str):
    page = (
        PublishedPage.objects
        .filter(slug=slug, is_listed=True, visibility='public')
        .first()
    )
    if page is None or not page.file:
        return _public_response({'error': 'Not found.'}, status_code=404)
    response = FileResponse(page.file.open('rb'), as_attachment=True,
                            filename=page.file_name or 'file')
    response['Content-Security-Policy'] = CSP_HEADER
    return response


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def page_download(request, slug: str):
    page = PublishedPage.objects.filter(slug=slug).first()
    if page is None or not _visible_to(page, request.user) or not page.file:
        return Response({'error': 'Not found.'},
                        status=status.HTTP_404_NOT_FOUND)
    return FileResponse(page.file.open('rb'), as_attachment=True,
                        filename=page.file_name or 'file')
