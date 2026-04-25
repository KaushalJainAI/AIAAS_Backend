"""
Inference App API Views — Documents, Knowledge Bases, and RAG Endpoints
"""
import threading
import logging
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from django.shortcuts import get_object_or_404
from django.utils import timezone
from asgiref.sync import sync_to_async

from adrf.decorators import api_view
from rest_framework.decorators import permission_classes

from .models import Document, KnowledgeBase
from .engine import get_hnsw_kb, get_kb_manager, get_rag_pipeline
from .utils import validate_file_upload
from .serializers import (
    DocumentSerializer, KnowledgeBaseSerializer,
    RagSearchSerializer, RagQuerySerializer,
)
from django.core.exceptions import ValidationError

logger = logging.getLogger(__name__)


# =============================================================================
# Knowledge Base CRUD
# =============================================================================

@api_view(['GET', 'POST'])
@permission_classes([IsAuthenticated])
async def kb_list(request):
    """
    GET: List all KBs for the authenticated user.
    POST: Create a new named KB.
    """
    if request.method == 'GET':
        def _list():
            kbs = KnowledgeBase.objects.filter(user=request.user).order_by('-created_at')
            return KnowledgeBaseSerializer(kbs, many=True).data

        return Response(await sync_to_async(_list)())

    # POST — create new KB
    name = (request.data.get('name') or '').strip()
    if not name:
        return Response({'error': 'name is required'}, status=400)

    description = request.data.get('description', '')

    def _create():
        if KnowledgeBase.objects.filter(user=request.user, name=name).exists():
            return None
        return KnowledgeBase.objects.create(
            user=request.user,
            name=name,
            description=description,
        )

    kb = await sync_to_async(_create)()
    if kb is None:
        return Response({'error': f'A knowledge base named "{name}" already exists.'}, status=400)
    return Response(KnowledgeBaseSerializer(kb).data, status=201)


@api_view(['GET', 'DELETE'])
@permission_classes([IsAuthenticated])
async def kb_detail(request, kb_id: int):
    """
    GET: Retrieve KB details (with document list).
    DELETE: Delete KB, remove its index from local disk + S3.
    """
    kb = await sync_to_async(get_object_or_404)(KnowledgeBase, id=kb_id, user=request.user)

    if request.method == 'GET':
        def _detail():
            docs = Document.objects.filter(knowledge_base=kb).order_by('-created_at')
            return {
                **KnowledgeBaseSerializer(kb).data,
                'documents': DocumentSerializer(docs, many=True).data,
            }
        return Response(await sync_to_async(_detail)())

    # DELETE
    def _delete():
        # Un-assign documents so they aren't orphaned references
        Document.objects.filter(knowledge_base=kb).update(knowledge_base=None)
        kb_id_local = kb.id
        s3_key = kb.s3_index_key
        kb.delete()
        return kb_id_local, s3_key

    deleted_id, s3_key = await sync_to_async(_delete)()

    # Remove from in-memory manager
    get_kb_manager().evict(deleted_id)

    # Clean up local files
    hnsw = get_hnsw_kb(deleted_id)
    hnsw.destroy_local()

    # Clean up S3
    if s3_key:
        import asyncio
        from .engine import _delete_from_s3
        await asyncio.to_thread(_delete_from_s3, s3_key + '.faiss')
        await asyncio.to_thread(_delete_from_s3, s3_key + '_docs.pkl')

    return Response(status=204)


@api_view(['POST'])
@permission_classes([IsAuthenticated])
async def kb_assign_document(request, kb_id: int, document_id: int):
    """Move a document into a different KB and re-index it."""
    kb = await sync_to_async(get_object_or_404)(KnowledgeBase, id=kb_id, user=request.user)
    doc = await sync_to_async(get_object_or_404)(Document, id=document_id, user=request.user)

    old_kb_id = doc.knowledge_base_id

    def _assign():
        doc.knowledge_base = kb
        doc.status = 'pending'
        doc.chunk_count = 0
        doc.save(update_fields=['knowledge_base', 'status', 'chunk_count'])

    await sync_to_async(_assign)()

    # Remove from old KB index
    if old_kb_id:
        old_hnsw = get_hnsw_kb(old_kb_id)
        await old_hnsw.initialize()
        await old_hnsw.delete_document(doc.id)

    # Re-index into new KB in background
    from .tasks import process_document
    threading.Thread(target=process_document, args=(doc.id, kb.id)).start()

    return Response({'detail': f'Document queued for re-indexing into KB "{kb.name}".'})


@api_view(['DELETE'])
@permission_classes([IsAuthenticated])
async def kb_remove_document(request, kb_id: int, document_id: int):
    """Remove a document's vectors from a KB without deleting the document."""
    kb = await sync_to_async(get_object_or_404)(KnowledgeBase, id=kb_id, user=request.user)
    doc = await sync_to_async(get_object_or_404)(Document, id=document_id, user=request.user, knowledge_base=kb)

    hnsw = get_hnsw_kb(kb.id, kb.s3_index_key or f'indices/kb_{kb.id}')
    await hnsw.initialize()
    await hnsw.delete_document(doc.id)

    def _unassign():
        doc.knowledge_base = None
        doc.chunk_count = 0
        doc.status = 'pending'
        doc.save(update_fields=['knowledge_base', 'chunk_count', 'status'])
        KnowledgeBase.objects.filter(id=kb.id).update(
            doc_count=Document.objects.filter(knowledge_base_id=kb.id).count(),
            vector_count=hnsw.ntotal,
            index_size_bytes=hnsw.index_size_bytes,
        )

    await sync_to_async(_unassign)()
    return Response(status=204)


# =============================================================================
# Documents
# =============================================================================

@api_view(['GET', 'POST'])
@permission_classes([IsAuthenticated])
async def document_list(request):
    """
    GET: List user's documents (personal + public).
    POST: Upload new document (optionally specify kb_id).
    """
    if request.method == 'GET':
        def _get():
            my_docs = Document.objects.filter(user=request.user)\
                .select_related('user', 'knowledge_base').order_by('-created_at')
            public_docs = Document.objects.filter(sharing_mode__in=['shared_read', 'shared_write'])\
                .select_related('user', 'knowledge_base').order_by('-shared_at')
            return {
                'my_documents': DocumentSerializer(my_docs, many=True).data,
                'public_documents': DocumentSerializer(public_docs, many=True).data,
            }
        return Response(await sync_to_async(_get)())

    # POST — upload
    if 'file' not in request.FILES:
        return Response({'error': 'No file provided.'}, status=400)

    file = request.FILES['file']
    try:
        await sync_to_async(validate_file_upload)(file)
    except ValidationError as e:
        return Response({'error': str(e)}, status=400)

    file_type = file.name.split('.')[-1].lower() if '.' in file.name else 'txt'
    kb_id = request.data.get('kb_id')

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
        ), kb.id

    doc, resolved_kb_id = await sync_to_async(_create)()
    threading.Thread(target=__import__('inference.tasks', fromlist=['process_document']).process_document,
                     args=(doc.id, resolved_kb_id)).start()

    return Response(DocumentSerializer(doc).data, status=201)


@api_view(['GET', 'DELETE'])
@permission_classes([IsAuthenticated])
async def document_detail(request, document_id: int):
    doc = await sync_to_async(get_object_or_404)(Document, id=document_id, user=request.user)

    if request.method == 'GET':
        return Response(DocumentSerializer(doc).data)

    # DELETE — also remove vectors from its KB
    kb_id = doc.knowledge_base_id
    doc_id = doc.id
    await sync_to_async(doc.delete)()

    if kb_id:
        hnsw = get_hnsw_kb(kb_id)
        await hnsw.initialize()
        await hnsw.delete_document(doc_id)

    return Response(status=204)


@api_view(['POST'])
@permission_classes([IsAuthenticated])
async def document_share(request, document_id: int):
    doc = await sync_to_async(get_object_or_404)(Document, id=document_id, user=request.user)

    if doc.sharing_mode == 'private':
        doc.sharing_mode = 'shared_read'
        doc.is_shared = True
        doc.shared_at = timezone.now()
        from .tasks import share_document
        threading.Thread(target=share_document, args=(doc.id, request.user.id)).start()
    else:
        return Response({
            **DocumentSerializer(doc).data,
            'error': 'Un-sharing documents is not allowed once they are part of the platform knowledge base.',
        }, status=403)

    await sync_to_async(doc.save)()
    return Response({**DocumentSerializer(doc).data, 'message': f'Document set to {doc.sharing_mode}'})


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

    if kb_id:
        kb_model = await sync_to_async(get_object_or_404)(KnowledgeBase, id=kb_id, user=request.user)
        hnsw = get_hnsw_kb(kb_model.id, kb_model.s3_index_key or f'indices/kb_{kb_model.id}')
        await hnsw.initialize()
        user_results = await hnsw.search(query, top_k=top_k)
    else:
        from .engine import get_kb_for_user
        _, hnsw = await get_kb_for_user(request.user.id)
        user_results = await hnsw.search(query, top_k=top_k)

    platform_results = []
    if data.get('include_platform'):
        from .engine import get_platform_knowledge_base
        platform_kb = get_platform_knowledge_base()
        await platform_kb.initialize()
        platform_results = await platform_kb.search(query, top_k=top_k)

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

    pipeline = get_rag_pipeline(user_id=request.user.id)
    await pipeline.kb.initialize()
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


@api_view(['GET'])
@permission_classes([IsAuthenticated])
async def document_download(request, document_id: int):
    doc = await sync_to_async(get_object_or_404)(Document, id=document_id, user=request.user)
    if doc.file:
        try:
            return FileResponse(doc.file.open('rb'), as_attachment=True, filename=doc.name)
        except Exception:
            pass
    buffer = BytesIO(doc.content_text.encode('utf-8'))
    return FileResponse(buffer, as_attachment=True, filename=doc.name)
