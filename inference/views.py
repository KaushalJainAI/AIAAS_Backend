"""
Inference App API Views — Documents, Knowledge Bases, and RAG Endpoints
"""
import threading
import logging
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from django.shortcuts import get_object_or_404
from django.utils import timezone
from asgiref.sync import sync_to_async

from adrf.decorators import api_view
from rest_framework.decorators import permission_classes
from drf_spectacular.utils import extend_schema, inline_serializer
from drf_spectacular.types import OpenApiTypes

from core.http.pagination import paginate_keyset
from . import filesystem as fs
from . import recycle
from .models import Document, KnowledgeBase
from .engine import KnowledgeBaseUnavailable, get_hnsw_kb, get_rag_pipeline
from .utils import normalize_file_type, validate_file_upload
from .serializers import (
    DocumentSerializer, DocumentListSerializer,
    RagSearchSerializer, RagQuerySerializer,
)
from django.core.exceptions import ValidationError

logger = logging.getLogger(__name__)


# =============================================================================
# Documents
# =============================================================================

# -- GET /api/documents/ -- the two response shapes ---------------------------
#
# `_legacy_page` is the uncursored shape older clients still ask for;
# `_cursor_page` is the keyset-paginated one. Both were inline branches of a
# nested closure, which is how they came to disagree on ordering and return the
# same rows in two different orders depending only on whether `limit` was
# passed. Named and separate, that class of drift is visible.

_LEGACY_CAP = 50
_DEFAULT_LIMIT = 50
_MAX_LIMIT = 100
_SHARED_MODES = ['shared_read', 'shared_write']


#: "the caller did not ask to filter by folder" — distinct from None, which is
#: the root folder. See `_owned_documents`.
_UNFILTERED = object()


def _owned_documents(user, folder=_UNFILTERED):
    """The caller's documents, optionally narrowed to one folder.

    `folder` defaults to the `_UNFILTERED` sentinel rather than None, because
    None is a *meaningful* location — the user's root — not "no filter". Absent
    means today's flat listing across the whole tree, which is what keeps the
    existing clients and tests working unchanged.
    """
    qs = (Document.objects.filter(user=user)
          .select_related('user', 'knowledge_base', 'folder')
          .order_by('-created_at'))
    if folder is not _UNFILTERED:
        qs = qs.filter(folder=folder)
    return qs


def _shared_documents():
    return (Document.objects.filter(sharing_mode__in=_SHARED_MODES)
            .select_related('user', 'knowledge_base').order_by('-created_at'))


def _wants_cursor_page(params) -> bool:
    return any(k in params for k in
               ('limit', 'cursor', 'my_cursor', 'public_cursor', 'scope'))


def _requested_limit(params) -> int:
    try:
        return min(max(int(params.get('limit', _DEFAULT_LIMIT)), 1), _MAX_LIMIT)
    except (TypeError, ValueError):
        return _DEFAULT_LIMIT


def _legacy_page(user, folder=_UNFILTERED) -> dict:
    """The uncursored shape, capped.

    `DocumentSerializer` exposes `content` -- each document's full extracted
    text -- so before the cap this response carried every character of every
    document the user owns plus every shared one, in a single list call.
    Callers needing more pass `limit`/`cursor` and get the paged shape.
    """
    mine = list(_owned_documents(user, folder)[:_LEGACY_CAP])
    shared = list(_shared_documents()[:_LEGACY_CAP])
    return {
        'my_documents': DocumentSerializer(mine, many=True).data,
        'public_documents': DocumentSerializer(shared, many=True).data,
        'truncated': len(mine) == _LEGACY_CAP or len(shared) == _LEGACY_CAP,
    }


def _cursor_page(user, params, folder=_UNFILTERED) -> dict:
    """The keyset-paginated shape.

    `scope` selects which half is paged; the unselected half stays None rather
    than becoming an empty page, so a caller can tell "you did not ask for
    this" from "you asked and there is nothing".
    """
    limit = _requested_limit(params)
    scope = params.get('scope', 'all')
    my_cursor = params.get('my_cursor') or params.get('cursor')
    public_cursor = params.get('public_cursor')

    my_page = public_page = None
    if scope != 'public':
        my_page = paginate_keyset(_owned_documents(user, folder), limit=limit, cursor=my_cursor)
    if scope != 'personal':
        public_page = paginate_keyset(
            _shared_documents(), limit=limit,
            cursor=(my_cursor or public_cursor) if scope == 'public' else public_cursor,
        )

    # The bare `next_cursor`/`has_more` keys are the single-scope aliases older
    # callers read. One page is authoritative for them: the public one when
    # that is all that was asked for, otherwise the user's own.
    primary = public_page if scope == 'public' else my_page
    return {
        'my_documents': DocumentListSerializer(my_page.items, many=True).data if my_page else [],
        'public_documents': DocumentListSerializer(public_page.items, many=True).data if public_page else [],
        'my_next_cursor': my_page.next_cursor if my_page else None,
        'public_next_cursor': public_page.next_cursor if public_page else None,
        'my_has_more': my_page.has_more if my_page else False,
        'public_has_more': public_page.has_more if public_page else False,
        'next_cursor': primary.next_cursor if primary else None,
        'has_more': primary.has_more if primary else False,
        'limit': limit,
    }


def _document_page(request) -> dict:
    params = request.query_params
    # `folder_id` narrows the personal half to one folder; `root` means the
    # documents sitting directly at the user's root. Absent leaves the listing
    # flat, exactly as before this feature existed. Shared documents are never
    # narrowed — they are a flat public library, not a place in anyone's tree.
    folder = _UNFILTERED
    if 'folder_id' in params:
        folder = fs.resolve_folder(request.user, params.get('folder_id'))

    if _wants_cursor_page(params):
        return _cursor_page(request.user, params, folder)
    return _legacy_page(request.user, folder)


@extend_schema(
    methods=['GET'],
    responses={200: inline_serializer(
        name="DocumentListResponse",
        fields={
            "my_documents": DocumentSerializer(many=True),
            "public_documents": DocumentSerializer(many=True),
        },
    )},
)
@extend_schema(
    methods=['POST'],
    responses={201: DocumentSerializer, 400: OpenApiTypes.OBJECT},
)
@api_view(['GET', 'POST'])
@permission_classes([IsAuthenticated])
async def document_list(request):
    """
    GET: List user's documents (personal + public).
    POST: Upload new document (optionally specify kb_id).
    """
    if request.method == 'GET':
        try:
            return Response(await sync_to_async(_document_page)(request))
        except fs.FolderNotFound as exc:
            return Response({'error': str(exc)}, status=404)

    # POST — upload
    if 'file' not in request.FILES:
        return Response({'error': 'No file provided.'}, status=400)

    file = request.FILES['file']
    try:
        mime_type = await sync_to_async(validate_file_upload)(file)
    except ValidationError as e:
        return Response({'error': str(e)}, status=400)

    # One vocabulary for file_type across every producer — the raw extension
    # was not one of Document.FILE_TYPE_CHOICES, so images were indexed as
    # mojibake and never reached the vision path.
    file_type = normalize_file_type(file.name, mime_type)
    kb_id = request.data.get('kb_id')

    # Where the file lands in the caller's tree. Absent means their root, which
    # is what keeps every existing client working unchanged. Resolved through
    # the one choke point, so a foreign id is a 404 rather than a filing.
    try:
        folder = await sync_to_async(fs.resolve_folder)(
            request.user, request.data.get('folder_id'))
    except fs.FolderNotFound as exc:
        return Response({'error': str(exc)}, status=404)

    def _create():
        kb = None
        if kb_id:
            try:
                kb = KnowledgeBase.objects.get(id=int(kb_id), user=request.user)
            except (KnowledgeBase.DoesNotExist, ValueError):
                pass
        if kb is None:
            kb, _ = KnowledgeBase.objects.get_or_create(
                user=request.user,
                is_default=True,
                defaults={'name': 'Default', 'description': 'Auto-created default knowledge base'},
            )
        return Document.objects.create(
            user=request.user,
            name=file.name,
            content_text='',
            file=file,
            file_type=file_type,
            file_size=file.size,
            status='pending',
            knowledge_base=kb,
            folder=folder,
        ), kb.id

    doc, resolved_kb_id = await sync_to_async(_create)()
    # Index inline in a background thread. There is no Celery worker / Redis
    # broker on this deployment, so .delay() would hang on broker-reconnect and
    # then fail — leaving the upload stuck "pending". The thread runs the same
    # sync indexing service used by the Celery path.
    from .tasks import process_document
    threading.Thread(
        target=process_document, args=(doc.id, resolved_kb_id), daemon=True
    ).start()

    return Response(DocumentSerializer(doc).data, status=201)


@api_view(['GET', 'DELETE'])
@permission_classes([IsAuthenticated])
async def document_detail(request, document_id: int):
    doc = await sync_to_async(
        lambda: get_object_or_404(
            Document.objects.select_related('user', 'knowledge_base'),
            id=document_id, user=request.user,
        )
    )()

    if request.method == 'GET':
        # Serialize in a sync context: the serializer touches obj.user /
        # obj.knowledge_base, which would trigger a lazy DB query from this
        # async view and raise SynchronousOnlyOperation.
        return Response(await sync_to_async(lambda: DocumentSerializer(doc).data)())

    # DELETE — to the recycle bin, not out of existence. The row keeps its
    # `content_text` and its file, so restore is just a re-ingest through the
    # ordinary upload door; `recycle.trash` drops the vectors immediately,
    # because a file the user can no longer see must not keep answering RAG
    # queries. The permanent delete happens in `manage.py purge_recycle_bin`
    # / the `inference.sweep_recycle_bin` beat task, after the retention.
    result = await sync_to_async(recycle.trash)(request.user, documents=[doc])
    return Response(result, status=200)


@api_view(['POST'])
@permission_classes([IsAuthenticated])
async def document_share(request, document_id: int):
    doc = await sync_to_async(
        lambda: get_object_or_404(
            Document.objects.select_related('user', 'knowledge_base'),
            id=document_id, user=request.user,
        )
    )()

    if doc.sharing_mode != 'private':
        # Serialize in a sync context (obj.user / obj.knowledge_base are lazy).
        data = await sync_to_async(lambda: DocumentSerializer(doc).data)()
        return Response({
            **data,
            'error': 'Un-sharing documents is not allowed once they are part of the platform knowledge base.',
        }, status=403)

    doc.sharing_mode = 'shared_read'
    doc.is_shared = True
    doc.shared_at = timezone.now()

    # Commit *before* the worker starts. The thread re-reads the row to copy
    # `sharing_mode` into the platform KB's metadata, so starting it first was
    # a race it could lose — recording the document as still private.
    await sync_to_async(doc.save)()

    # Inline background thread — no Celery worker on this box (see upload).
    from .tasks import share_document
    threading.Thread(
        target=share_document, args=(doc.id, request.user.id), daemon=True
    ).start()

    data = await sync_to_async(lambda: DocumentSerializer(doc).data)()
    return Response({**data, 'message': f'Document set to {doc.sharing_mode}'})


# =============================================================================
# RAG search / query endpoints
# =============================================================================

@api_view(['POST'])
@permission_classes([IsAuthenticated])
async def rag_search(request):
    serializer = RagSearchSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    data = serializer.validated_data

    query = data['query']
    top_k = data['top_k']
    kb_id = data.get('kb_id')

    # A KB that cannot be opened answers 503, not an empty result list: a
    # broken embedder and an empty corpus must not look the same to the caller.
    try:
        if kb_id:
            kb_model = await sync_to_async(get_object_or_404)(KnowledgeBase, id=kb_id, user=request.user)
            hnsw = get_hnsw_kb(kb_model.id, kb_model.s3_index_key or f'indices/kb_{kb_model.id}')
            await hnsw.initialize()
        else:
            from .engine import get_kb_for_user
            _, hnsw = await get_kb_for_user(request.user.id)

        # Embed the question once and reuse it for every tier searched. The two
        # searches asked the same embedder the same question and paid for it twice.
        query_emb = await hnsw.embed_query(query)
        user_results = await hnsw.search(query, top_k=top_k, query_embedding=query_emb)

        platform_results = []
        if data.get('include_platform'):
            from .engine import get_platform_knowledge_base
            platform_kb = get_platform_knowledge_base()
            await platform_kb.initialize()
            platform_results = await platform_kb.search(
                query, top_k=top_k, query_embedding=query_emb
            )
    except KnowledgeBaseUnavailable as exc:
        logger.error('rag_search could not open a knowledge base: %s', exc)
        return Response({'error': str(exc)}, status=503)

    return Response({
        'query': query,
        'results': [
            {'document_id': r.document_id, 'content': r.content, 'score': r.score, 'source': 'personal', 'is_image': r.is_image}
            for r in user_results
        ],
        'platform_results': [
            {'document_id': r.document_id, 'content': r.content, 'score': r.score, 'source': 'platform'}
            for r in platform_results
        ],
    })


@api_view(['POST'])
@permission_classes([IsAuthenticated])
async def rag_query(request):
    serializer = RagQuerySerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    data = serializer.validated_data

    try:
        pipeline = await get_rag_pipeline(user_id=request.user.id)
        await pipeline.kb.initialize()
    except KnowledgeBaseUnavailable as exc:
        logger.error('rag_query could not open a knowledge base: %s', exc)
        return Response({'error': str(exc)}, status=503)

    result = await pipeline.query(
        question=data['question'],
        user_id=request.user.id,
        llm_type=data['llm_type'],
        top_k=data['top_k'],
        credential_id=data.get('credential_id'),
    )
    return Response(result)


# =============================================================================
# Document download
# =============================================================================

from django.http import FileResponse
from io import BytesIO


def _servable(doc) -> bool:
    """Whether this document's bytes may be streamed back.

    New uploads are written by `utils.user_document_path`, whose every segment
    is server-derived — but rows predate it, and a `FileField` name is just a
    string in a column. `validate_attachment_path` is the guard already used
    for LLM attachments; reusing it here closes the one traversal gap the
    2026-08-24 audit found in this view. Remote storage has no local path, so
    absence of one is not a failure.
    """
    from django.core.exceptions import SuspiciousFileOperation

    from llm.handlers.openai_compatible import validate_attachment_path

    try:
        path = doc.file.path
    except SuspiciousFileOperation:
        # Django's storage refused to even build the path. That is already the
        # right answer; catching it here turns a 400 from deep inside the
        # storage layer into the ordinary content_text fallback.
        logger.error('Refused to serve document %s: storage rejected its path', doc.pk)
        return False
    except (NotImplementedError, ValueError):
        return True          # non-filesystem storage — nothing to traverse
    if not validate_attachment_path(path):
        logger.error(
            'Refused to serve document %s: %s is outside MEDIA_ROOT', doc.pk, path,
        )
        return False
    return True


@api_view(['GET'])
@permission_classes([IsAuthenticated])
async def document_download(request, document_id: int):
    doc = await sync_to_async(get_object_or_404)(Document, id=document_id, user=request.user)
    if doc.file and await sync_to_async(_servable)(doc):
        try:
            return FileResponse(doc.file.open('rb'), as_attachment=True, filename=doc.name)
        except Exception:
            pass
    buffer = BytesIO(doc.content_text.encode('utf-8'))
    return FileResponse(buffer, as_attachment=True, filename=doc.name)
