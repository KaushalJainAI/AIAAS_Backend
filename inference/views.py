"""
Inference App API Views - Documents and RAG Endpoints

Using ADRF (Async Django REST Framework) for proper async support.

INSTALLATION REQUIRED:
    pip install adrf
"""
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from django.shortcuts import get_object_or_404
from django.utils import timezone
from asgiref.sync import sync_to_async

# IMPORTANT: Use ADRF's api_view decorator for async support
from adrf.decorators import api_view
from rest_framework.decorators import permission_classes

from .models import Document
from .engine import (
    get_user_knowledge_base, 
    get_platform_knowledge_base,
    get_rag_pipeline
)


@api_view(['GET', 'POST'])
@permission_classes([IsAuthenticated])
async def document_list(request):
    """
    GET: List user's documents
    POST: Upload new document
    
    Now properly async with ADRF!
    """
    if request.method == 'GET':
        # Wrap database query in sync_to_async
        docs = await sync_to_async(list)(
            Document.objects.filter(user=request.user).order_by('-created_at')
        )
        
        return Response({
            'documents': [
                {
                    'id': d.id,
                    'title': d.name,
                    'filename': d.name,
                    'file_type': d.file_type,
                    'file_size': d.file_size,
                    'chunk_count': d.chunk_count,
                    'is_shared': d.is_shared,
                    'shared_at': d.shared_at,
                    'created_at': d.created_at,
                    'updated_at': d.updated_at,
                }
                for d in docs
            ]
        })
    
    elif request.method == 'POST':
        name = request.data.get('name', 'Untitled')
        content = request.data.get('content', '')
        file_type = request.data.get('file_type', 'text')
        
        # Handle file upload without blocking
        if 'file' in request.FILES:
            file = request.FILES['file']
            name = file.name
            # Read file asynchronously
            content = await sync_to_async(file.read)()
            content = content.decode('utf-8', errors='ignore')
            file_type = name.split('.')[-1] if '.' in name else 'txt'
        
        # Create document without blocking
        doc = await sync_to_async(Document.objects.create)(
            user=request.user,
            name=name,
            content_text=content,
            file_type=file_type,
            file_size=len(content),
            status='pending'  # Explicitly set status to pending
        )
        
        # Trigger background processing via Thread (no Redis required)
        import threading
        from .tasks import process_document
        threading.Thread(target=process_document, args=(doc.id,)).start()
        
        return Response({
            'id': doc.id,
            'title': doc.name,
            'filename': doc.name,
            'file_type': doc.file_type,
            'file_size': doc.file_size,
            'chunk_count': 0,
            'is_shared': doc.is_shared,
            'created_at': doc.created_at,
            'updated_at': doc.updated_at,
            'status': 'pending', 
        }, status=status.HTTP_201_CREATED)


@api_view(['GET', 'DELETE'])
@permission_classes([IsAuthenticated])
async def document_detail(request, document_id: int):
    """
    GET: Get document details
    DELETE: Delete document
    """
    # Wrap get_object_or_404 in sync_to_async
    doc = await sync_to_async(get_object_or_404)(
        Document, id=document_id, user=request.user
    )
    
    if request.method == 'GET':
        return Response({
            'id': doc.id,
            'title': doc.name,
            'filename': doc.name,
            'content': doc.content_text,
            'file_type': doc.file_type,
            'file_size': doc.file_size,
            'chunk_count': doc.chunk_count,
            'is_shared': doc.is_shared,
            'shared_at': doc.shared_at,
            'metadata': doc.metadata,
            'created_at': doc.created_at,
            'updated_at': doc.updated_at,
        })
    
    elif request.method == 'DELETE':
        # Delete asynchronously
        await sync_to_async(doc.delete)()
        return Response(status=status.HTTP_204_NO_CONTENT)


@api_view(['POST'])
@permission_classes([IsAuthenticated])
async def document_share(request, document_id: int):
    """
    Toggle document sharing with platform knowledge base.
    """
    doc = await sync_to_async(get_object_or_404)(
        Document, id=document_id, user=request.user
    )
    
    # Toggle sharing status
    doc.is_shared = not doc.is_shared
    
    if doc.is_shared:
        doc.shared_at = timezone.now()
        
        try:
            platform_kb = get_platform_knowledge_base()
            await platform_kb.initialize()
            await platform_kb.add_document(doc.id, doc.content_text, {
                'name': doc.name,
                'user_id': request.user.id,
                'shared': True,
            })
        except Exception as e:
            return Response({
                'error': f'Failed to share document: {str(e)}'
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
    else:
        doc.shared_at = None
    
    await sync_to_async(doc.save)()
    
    return Response({
        'id': doc.id,
        'is_shared': doc.is_shared,
        'shared_at': doc.shared_at,
        'message': 'Document shared with platform' if doc.is_shared else 'Document unshared from platform'
    })


@api_view(['POST'])
@permission_classes([IsAuthenticated])
async def rag_search(request):
    """
    Search documents using RAG.
    """
    query = request.data.get('query', '')
    top_k = int(request.data.get('top_k', 5))
    include_platform = request.data.get('include_platform', False)
    
    if not query:
        return Response({'error': 'Query is required'}, status=400)
    
    # Search user's personal KB
    user_kb = get_user_knowledge_base(request.user.id)
    await user_kb.initialize()
    user_results = await user_kb.search(query, top_k=top_k)
    
    # Optionally also search platform KB
    platform_results = []
    if include_platform:
        platform_kb = get_platform_knowledge_base()
        await platform_kb.initialize()
        platform_results = await platform_kb.search(query, top_k=top_k)
    
    return Response({
        'query': query,
        'results': [
            {
                'document_id': r.document_id,
                'content': r.content,
                'score': r.score,
                'source': 'personal',
            }
            for r in user_results
        ],
        'platform_results': [
            {
                'document_id': r.document_id,
                'content': r.content,
                'score': r.score,
                'source': 'platform',
            }
            for r in platform_results
        ] if include_platform else []
    })


@api_view(['POST'])
@permission_classes([IsAuthenticated])
async def rag_query(request):
    """
    Ask a question using RAG pipeline.
    """
    question = request.data.get('question', '')
    llm_type = request.data.get('llm_type', 'openai')
    credential_id = request.data.get('credential_id')
    top_k = int(request.data.get('top_k', 5))
    
    if not question:
        return Response({'error': 'Question is required'}, status=400)
    
    # Use user-specific RAG pipeline
    pipeline = get_rag_pipeline(user_id=request.user.id)
    await pipeline.kb.initialize()
    result = await pipeline.query(
        question=question,
        user_id=request.user.id,
        llm_type=llm_type,
        top_k=top_k,
        credential_id=credential_id,
    )
    
    return Response(result)