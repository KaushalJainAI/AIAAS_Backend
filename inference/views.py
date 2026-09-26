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
from .utils import harden_file_response, normalize_file_type, validate_file_upload
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


def _owned_documents(user, folder=_UNFILTERED, types=None):
    """The caller's documents, optionally narrowed to one folder.

    `folder` defaults to the `_UNFILTERED` sentinel rather than None, because
    None is a *meaningful* location — the user's root — not "no filter". Absent
    means today's flat listing across the whole tree, which is what keeps the
    existing clients and tests working unchanged.
    """
    qs = (Document.objects.filter(user=user)
          .select_related('user', 'knowledge_base', 'folder')
          .order_by('-created_at'))
    # The hidden eval tree never lists: fixture documents are working data
    # for the eval harness, not the owner's files (see
    # `filesystem.EVAL_ROOT_NAME`). Id-addressed reads still work.
    hidden = fs.eval_subtree_ids(user)
    if hidden:
        qs = qs.exclude(folder_id__in=hidden)
    if folder is not _UNFILTERED:
        qs = qs.filter(folder=folder)
    if types:
        qs = qs.filter(file_type__in=types)
    return qs


def _shared_documents():
    return (Document.objects.filter(sharing_mode__in=_SHARED_MODES)
            .select_related('user', 'knowledge_base').order_by('-created_at'))


def _wants_cursor_page(params) -> bool:
    return any(k in params for k in
               ('limit', 'cursor', 'my_cursor', 'public_cursor', 'scope', 'types'))


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


def _requested_types(params) -> list[str] | None:
    """`types=csv,xlsx` narrows the caller's own files to those file types.

    What the Apps launcher asks for: "every spreadsheet I have, wherever it
    is". Filtering in the browser meant paging the whole library to find them.
    """
    raw = params.get('types')
    if not raw:
        return None
    types = [t.strip().lower() for t in str(raw).split(',') if t.strip()]
    return types[:20] or None


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
        my_page = paginate_keyset(_owned_documents(user, folder, _requested_types(params)),
                                  limit=limit, cursor=my_cursor)
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


def _search_page(request) -> dict:
    from . import search as doc_search

    params = request.query_params
    scope = 'public' if params.get('scope') == 'public' else 'personal'
    folder = doc_search.EVERYWHERE
    if scope == 'personal' and params.get('folder_id') not in (None, ''):
        folder = fs.resolve_folder(request.user, params.get('folder_id'))

    found = doc_search.search(
        request.user, params.get('q', ''), folder=folder, scope=scope,
        types=_requested_types(params),
        limit=params.get('limit', doc_search.DEFAULT_LIMIT),
    )

    def rows(docs):
        out = DocumentListSerializer(docs, many=True).data
        for row in out:
            row['matched_in'] = found['matched_in'].get(row['id'])
            row['snippet'] = found['snippets'].get(row['id'])
            if row['id'] in found['scores']:
                row['score'] = found['scores'][row['id']]
        return out

    exact, fuzzy = rows(found['exact']), rows(found['fuzzy'])
    folders = [
        {'id': f.id, 'name': f.name, 'parent_id': f.parent_id,
         'location': fs.name_path(f.parent) if f.parent_id else '/',
         'updated_at': f.updated_at, 'matched_in': how}
        for f, how in found['folders']
    ]
    return {
        'query': found['query'],
        'exact': exact,
        'fuzzy': fuzzy,
        'folders': folders,
        'count': len(exact) + len(fuzzy),
        'truncated': found['truncated'],
        'note': ('More files match than are shown. Keep typing to narrow it down.'
                 if found['truncated'] else None),
    }


@extend_schema(responses={200: OpenApiTypes.OBJECT, 404: OpenApiTypes.OBJECT})
@api_view(['GET'])
@permission_classes([IsAuthenticated])
async def document_search(request):
    """Search the caller's files by name and contents, with close matches by
    name when nothing matches exactly. See `inference/search.py`.

    A query under two characters answers 200 with nothing, never 400 — the
    caller is a search box being typed into.
    """
    try:
        return Response(await sync_to_async(_search_page)(request))
    except fs.FolderNotFound as exc:
        return Response({'error': str(exc)}, status=404)


def _readable_document(user, document_id: int) -> Document:
    """The caller's own document, or one shared into the public library.

    Reading is wider than writing on purpose: the Documents page lists the
    public library, and a listed file that 404s when opened is the preview
    failing for reasons the reader cannot see. Every write stays owner-only.
    """
    from django.db.models import Q

    return get_object_or_404(
        Document.objects.select_related('user', 'knowledge_base', 'folder'),
        Q(user=user) | Q(sharing_mode__in=_SHARED_MODES),
        id=document_id,
    )


def _owned_document(user, document_id: int) -> Document:
    return get_object_or_404(
        Document.objects.select_related('user', 'knowledge_base', 'folder'),
        id=document_id, user=user,
    )


@api_view(['GET', 'PATCH', 'DELETE'])
@permission_classes([IsAuthenticated])
async def document_detail(request, document_id: int):
    if request.method == 'GET':
        doc = await sync_to_async(_readable_document)(request.user, document_id)
        # Serialize in a sync context: the serializer touches obj.user /
        # obj.knowledge_base, which would trigger a lazy DB query from this
        # async view and raise SynchronousOnlyOperation.
        return Response(await sync_to_async(lambda: DocumentSerializer(doc).data)())

    doc = await sync_to_async(_owned_document)(request.user, document_id)

    if request.method == 'PATCH':
        # Rename -- the one metadata change the file browser needs. Moving is
        # `fs/move/`, which already handles files and folders in one request.
        from . import office_edit

        def _rename():
            office_edit.rename(doc, request.data.get('name'))
            return DocumentSerializer(doc).data

        try:
            return Response(await sync_to_async(_rename)())
        except office_edit.EditError as exc:
            return Response({'error': str(exc)}, status=exc.status)

    # DELETE -- to the recycle bin, not out of existence. The row keeps its
    # `content_text` and its file, so restore is just a re-ingest through the
    # ordinary upload door; `recycle.trash` drops the vectors immediately,
    # because a file the user can no longer see must not keep answering RAG
    # queries. The permanent delete happens in `manage.py purge_recycle_bin`
    # / the `inference.sweep_recycle_bin` beat task, after the retention.
    result = await sync_to_async(recycle.trash)(request.user, documents=[doc])
    return Response(result, status=200)


@api_view(['PATCH'])
@permission_classes([IsAuthenticated])
async def document_content(request, document_id: int):
    """Replace a text document's contents in-browser.

    Body: `{content: str, expected_updated_at?: str}`. `If-Match` header (or
    `expected_updated_at`) must equal the `updated_at` the detail view
    returned, else 412 — so an AI draft saved after the human opened it does
    not silently clobber the human's read. Text types only (`txt | md | csv |
    json | html`); a binary (`docx | xlsx | pptx | pdf | image | …` with bytes)
    is 400 — re-render it through the office tools instead of editing an
    extract that is not the file.
    """
    from .utils import TEXT_FILE_TYPES
    from workflow_backend.thresholds import AGENT_FILE_WRITE_CHARS

    doc = await sync_to_async(
        lambda: get_object_or_404(
            Document.objects.select_related('user', 'knowledge_base'),
            id=document_id, user=request.user,
        )
    )()

    if doc.file_type not in TEXT_FILE_TYPES or (doc.file and doc.file_type not in TEXT_FILE_TYPES):
        return Response(
            {'error': f'This is a {doc.file_type} file; its text is an extract, not the file. Re-render it instead of editing the extract.'},
            status=400,
        )

    content = request.data.get('content')
    if not isinstance(content, str):
        return Response({'error': 'Give `content` as a string.'}, status=400)
    if len(content) > AGENT_FILE_WRITE_CHARS:
        return Response(
            {'error': f'That is {len(content):,} characters; the limit for one save is {AGENT_FILE_WRITE_CHARS:,}.'},
            status=400,
        )

    from . import office_edit

    expected = request.headers.get('If-Match') or request.data.get('expected_updated_at')
    # Compared as instants: DRF writes UTC as `Z`, `isoformat()` as `+00:00`,
    # and a string compare refused every save the browser ever sent.
    if office_edit.is_stale(doc, expected):
        return Response(
            {'error': 'This file changed since you opened it. Re-open it and re-apply your change.',
             'updated_at': doc.updated_at.isoformat()},
            status=412,
        )

    def _save():
        office_edit.save_text(doc, content)
        return DocumentSerializer(doc).data

    return Response(await sync_to_async(_save)())


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
    # `?inline=1` serves bytes for in-browser preview (`Content-Disposition:
    # inline` + range-friendly FileResponse); default stays `as_attachment`
    # so existing Export flows keep forcing a save. Ownership is checked by
    # the `user=` lookup either way.
    inline = request.query_params.get('inline') == '1'
    doc = await sync_to_async(_readable_document)(request.user, document_id)
    # A pending office draft renders first: the download is the file, and the
    # file is what the draft is still building.
    from . import drafts

    doc = await sync_to_async(drafts.ensure_rendered)(doc)
    if doc.file and await sync_to_async(_servable)(doc):
        try:
            return harden_file_response(FileResponse(doc.file.open('rb'), as_attachment=not inline, filename=doc.name))
        except Exception:
            pass
    buffer = BytesIO(doc.content_text.encode('utf-8'))
    return harden_file_response(FileResponse(buffer, as_attachment=not inline, filename=doc.name))


# =============================================================================
# The productivity apps -- new files and office edits (inference/office_edit.py)
# =============================================================================

@api_view(['POST'])
@permission_classes([IsAuthenticated])
async def document_new(request):
    """Create a blank file (or a text file with `content`) in a folder.

    Body: `{name, folder_id?, content?}`. The extension decides the type; a
    taken name becomes `name (2).ext`. Not an upload, so nothing is indexed.
    """
    from . import office_edit

    try:
        folder = await sync_to_async(fs.resolve_folder)(
            request.user, request.data.get('folder_id'))
    except fs.FolderNotFound as exc:
        return Response({'error': str(exc)}, status=404)

    def _create():
        doc = office_edit.create(request.user, request.data.get('name'), folder,
                                 request.data.get('content') or '')
        return DocumentSerializer(doc).data

    try:
        return Response(await sync_to_async(_create)(), status=201)
    except office_edit.EditError as exc:
        return Response({'error': str(exc)}, status=exc.status)


@api_view(['GET', 'POST'])
@permission_classes([IsAuthenticated])
async def document_office(request, document_id: int):
    """Basic edits to an office file.

    GET (xlsx): every sheet's cells, formulas as their source.
    POST: `{set_cells?, append_rows?, sheet?}` for a workbook, or `{spec}` for
    a deck or Word file made in this workspace. Both take `expected_updated_at`
    (or `If-Match`) and answer 412 when the file changed since it was opened.
    """
    from . import office_edit

    if request.method == 'GET':
        doc = await sync_to_async(_readable_document)(request.user, document_id)
        # A pending office draft renders first: the grid is the file, and the
        # file is what the draft is still building.
        from . import drafts

        doc = await sync_to_async(drafts.ensure_rendered)(doc)
        try:
            return Response(await sync_to_async(office_edit.workbook_grid)(doc))
        except office_edit.EditError as exc:
            return Response({'error': str(exc)}, status=exc.status)

    doc = await sync_to_async(_owned_document)(request.user, document_id)
    expected = request.headers.get('If-Match') or request.data.get('expected_updated_at')
    if office_edit.is_stale(doc, expected):
        return Response(
            {'error': 'This file changed since you opened it. Re-open it and re-apply your change.',
             'updated_at': doc.updated_at.isoformat()},
            status=412,
        )

    def _apply():
        if 'spec' in request.data:
            office_edit.edit_spec(doc, request.data.get('spec'))
        else:
            office_edit.edit_workbook(doc, request.data)
        return DocumentSerializer(doc).data

    try:
        return Response(await sync_to_async(_apply)())
    except office_edit.EditError as exc:
        return Response({'error': str(exc)}, status=exc.status)


@api_view(['POST'])
@permission_classes([IsAuthenticated])
async def document_import(request, document_id: int):
    """Convert an uploaded Word/PowerPoint file into an editable one.

    Best effort and labelled as a conversion; the original upload stays
    version 1, extracted images are saved beside the document, and the
    response says what was lost. Takes `expected_updated_at` / `If-Match`
    like a save and answers 412 when stale.
    """
    from . import importers, office_edit

    doc = await sync_to_async(_owned_document)(request.user, document_id)
    expected = request.headers.get('If-Match') or request.data.get('expected_updated_at')
    if office_edit.is_stale(doc, expected):
        return Response(
            {'error': 'This file changed since you opened it. Reload it, then convert.',
             'updated_at': doc.updated_at.isoformat()},
            status=412,
        )

    def _convert():
        from .serializers import DocumentSerializer

        result = importers.import_upload(doc)
        return {**DocumentSerializer(doc).data, **result}

    try:
        return Response(await sync_to_async(_convert)())
    except importers.ImportError_ as exc:
        return Response({'error': str(exc)}, status=exc.status)


@api_view(['GET'])
@permission_classes([IsAuthenticated])
async def document_asset(request, document_id: int):
    """An image the file's spec embeds, through the owner's read-only scope.

    Only paths the spec itself names are served — this is how an editor shows
    its own figures, not a general image proxy.
    """
    from django.http import Http404

    from . import vfs as vfs_mod

    doc = await sync_to_async(_readable_document)(request.user, document_id)
    path = request.query_params.get('path')
    spec = (doc.metadata or {}).get('spec') or {}

    from office import deck as deck_tool
    from office import document as document_tool

    if doc.file_type == 'pptx':
        allowed = set(deck_tool.image_paths(spec))
    elif doc.file_type == 'docx':
        allowed = set(document_tool.image_paths(spec))
    else:
        allowed = set()
    if not path or path not in allowed:
        raise Http404()
    ext = (path.rsplit('.', 1)[-1].lower() if '.' in path else '')
    mime = {'png': 'image/png', 'jpg': 'image/jpeg', 'jpeg': 'image/jpeg',
            'gif': 'image/gif', 'bmp': 'image/bmp', 'webp': 'image/webp'}.get(ext)
    if mime is None:
        # Raster images only. Anything else (an SVG above all) would be
        # served inline on the API origin, where its script runs as us.
        raise Http404()

    def _load():
        scope = vfs_mod.build_scope(doc.user, 'readonly')
        return vfs_mod.read_image(scope, path)

    try:
        data, _shown = await sync_to_async(_load)()
    except vfs_mod.VfsError:
        raise Http404()
    response = FileResponse(BytesIO(data), filename=path.rsplit('/', 1)[-1],
                            content_type=mime)
    return harden_file_response(response)


@api_view(['POST'])
@permission_classes([IsAuthenticated])
async def document_images(request, document_id: int):
    """Save an image beside the document, for embedding in it (owner only).

    Multipart `file`; answers the spec path to store. The file browser shows
    these like any other image — they are ordinary files that happen to sit
    next to the document that embeds them.
    """
    from django.core.files.uploadedfile import UploadedFile

    from . import filesystem as fs
    from . import vfs as vfs_mod
    from .utils import normalize_file_type

    doc = await sync_to_async(_owned_document)(request.user, document_id)
    upload = request.FILES.get('file')
    if not isinstance(upload, UploadedFile):
        return Response({'error': 'Send the image as multipart `file`.'}, status=400)
    if normalize_file_type(upload.name, upload.content_type) != 'image':
        return Response({'error': f'{upload.name} is not an image.'}, status=400)

    def _store():
        scope = vfs_mod.build_scope(request.user, 'full')
        folder = fs.name_path(doc.folder)
        path = f'{folder}/{upload.name}' if folder != '/' else f'/{upload.name}'
        return vfs_mod.write_binary(scope, path, upload.read())

    try:
        result = await sync_to_async(_store)()
    except vfs_mod.VfsError as exc:
        return Response({'error': str(exc)}, status=400)
    return Response({'path': result['path']}, status=201)


@api_view(['GET'])
@permission_classes([IsAuthenticated])
async def document_preview_image(request, document_id: int):
    """A browser-proof PNG for a TIFF/BMP/HEIC image (Phase F).

    Readable by whoever may read the file. The download stays the original;
    this is only what the preview shows.
    """
    from . import previews

    doc = await sync_to_async(_readable_document)(request.user, document_id)
    try:
        data, mime = await sync_to_async(previews.preview_image)(doc)
    except previews.PreviewError as exc:
        return Response({'error': str(exc)}, status=exc.status)
    return harden_file_response(
        FileResponse(BytesIO(data), filename=f'{doc.name}.png', content_type=mime))


@api_view(['GET'])
@permission_classes([IsAuthenticated])
async def document_archive(request, document_id: int):
    """The files inside a zip archive: name, size and date each (Phase F).

    Readable by whoever may read the file. Entries are listed, never served —
    no route serves a zip entry's bytes.
    """
    from . import previews

    doc = await sync_to_async(_readable_document)(request.user, document_id)
    try:
        listing = await sync_to_async(previews.archive_listing)(doc)
    except previews.PreviewError as exc:
        return Response({'error': str(exc)}, status=exc.status)
    return Response(listing)


@api_view(['POST'])
@permission_classes([IsAuthenticated])
async def document_copy(request, document_id: int):
    """Duplicate one of the caller's files into `folder_id` (absent = root)."""
    from . import office_edit

    doc = await sync_to_async(_owned_document)(request.user, document_id)
    try:
        folder = await sync_to_async(fs.resolve_folder)(
            request.user, request.data.get('folder_id'))
    except fs.FolderNotFound as exc:
        return Response({'error': str(exc)}, status=404)

    def _copy():
        from . import drafts

        # A pending office draft renders first: the copy is the file, and the
        # file is what the draft is still building.
        return DocumentSerializer(office_edit.copy(drafts.ensure_rendered(doc), folder)).data

    try:
        return Response(await sync_to_async(_copy)(), status=201)
    except office_edit.EditError as exc:
        return Response({'error': str(exc)}, status=exc.status)


# =============================================================================
# Version history and export (inference/versions.py, inference/export.py)
# =============================================================================

def _owned_version(user, document_id: int, version_id: int):
    from .models import DocumentVersion

    return get_object_or_404(
        DocumentVersion.objects.select_related('document'),
        id=version_id, document_id=document_id, document__user=user,
    )


@api_view(['GET'])
@permission_classes([IsAuthenticated])
async def document_versions(request, document_id: int):
    """What the file held before each overwrite, newest first (owner only)."""
    from . import versions

    doc = await sync_to_async(_owned_document)(request.user, document_id)
    return Response({'versions': await sync_to_async(versions.listing)(doc)})


@api_view(['GET'])
@permission_classes([IsAuthenticated])
async def document_version_download(request, document_id: int, version_id: int):
    from . import versions

    version = await sync_to_async(_owned_version)(request.user, document_id, version_id)
    data = await sync_to_async(versions.version_bytes)(version)
    inline = request.query_params.get('inline') == '1'
    return harden_file_response(FileResponse(BytesIO(data), as_attachment=not inline, filename=version.name))


@api_view(['POST'])
@permission_classes([IsAuthenticated])
async def document_version_restore(request, document_id: int, version_id: int):
    """Put a version back. Takes `expected_updated_at` / `If-Match` like a save."""
    from . import office_edit, versions

    version = await sync_to_async(_owned_version)(request.user, document_id, version_id)
    doc = await sync_to_async(_owned_document)(request.user, document_id)
    expected = request.headers.get('If-Match') or request.data.get('expected_updated_at')
    if office_edit.is_stale(doc, expected):
        return Response(
            {'error': 'This file changed since you opened it. Reload it, then restore.',
             'updated_at': doc.updated_at.isoformat()},
            status=412,
        )

    def _restore():
        return DocumentSerializer(versions.restore(doc, version)).data

    try:
        return Response(await sync_to_async(_restore)())
    except office_edit.EditError as exc:
        return Response({'error': str(exc)}, status=exc.status)


@api_view(['POST'])
@permission_classes([IsAuthenticated])
async def document_draft(request, document_id: int):
    """Park an office autosave cheaply: `{spec}` for a deck / Word file made
    here, `{grid: {sheets}}` for a workbook. Stores the editor state at once
    (`metadata.draft`) and rebuilds the real bytes after
    `DRAFT_RENDER_QUIET_SECONDS` of quiet — or sooner on any read that needs
    them (`ensure_rendered`). Takes `expected_updated_at` / `If-Match` like a
    save and answers 412 when stale.
    """
    import asyncio

    from workflow_backend.background import spawn

    from . import drafts

    doc = await sync_to_async(_owned_document)(request.user, document_id)
    expected = request.headers.get('If-Match') or request.data.get('expected_updated_at')

    def _save():
        from .serializers import DocumentSerializer

        _doc, stamp = drafts.save_draft(doc, request.data, expected)
        return DocumentSerializer(_doc).data, stamp

    try:
        data, stamp = await sync_to_async(_save)()
    except drafts.DraftError as exc:
        if exc.status == 412:
            return Response(
                {'error': str(exc), 'updated_at': doc.updated_at.isoformat()},
                status=412,
            )
        return Response({'error': str(exc)}, status=exc.status)

    async def _render_after_quiet(doc_id: int, seen: str):
        await asyncio.sleep(drafts._limits())
        await sync_to_async(drafts.maybe_render_draft)(doc_id, seen)

    try:
        spawn(_render_after_quiet(document_id, stamp))
    except RuntimeError:
        # No running loop (a sync caller): the draft still renders on the
        # next read that needs the bytes.
        pass
    return Response(data)


@api_view(['GET'])
@permission_classes([IsAuthenticated])
async def document_export(request, document_id: int):
    """The file in another format: `?to=pdf|docx|md|txt|csv|xlsx`.

    Readable by whoever may read the file. Without `to` it answers the formats
    this file offers, which is what the File menu renders. Not `?format=`:
    DRF reserves that name for content negotiation and answers 404 on it
    before the view runs.
    """
    from . import drafts, export

    doc = await sync_to_async(_readable_document)(request.user, document_id)
    # A pending office draft renders first: the export is the file, and the
    # file is what the draft is still building.
    doc = await sync_to_async(drafts.ensure_rendered)(doc)
    fmt = request.query_params.get('to')
    if not fmt:
        return Response({'formats': list(export.formats_for(doc))})
    def _build():
        # Off the shared sync thread (a PDF render is slow), so the connection
        # this pool thread opens is closed here — nothing else ever would, and
        # each one held would be a slot gone from the 10-connection pool.
        from django.db import connections

        try:
            return export.build(doc, fmt)
        finally:
            connections.close_all()

    try:
        data, name, mime = await sync_to_async(_build, thread_sensitive=False)()
    except export.ExportError as exc:
        return Response({'error': str(exc)}, status=exc.status)
    return harden_file_response(
        FileResponse(BytesIO(data), as_attachment=True, filename=name, content_type=mime))
