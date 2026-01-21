"""
Inference App API Views - Documents and RAG Endpoints
"""
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from django.shortcuts import get_object_or_404

from .models import Document
from .engine import get_knowledge_base, get_rag_pipeline


@api_view(['GET', 'POST'])
@permission_classes([IsAuthenticated])
def document_list(request):
    """
    GET: List user's documents
    POST: Upload new document
    """
    if request.method == 'GET':
        docs = Document.objects.filter(user=request.user).order_by('-created_at')
        return Response([
            {
                'id': d.id,
                'name': d.name,
                'file_type': d.file_type,
                'file_size': d.file_size,
                'chunk_count': d.chunk_count,
                'is_indexed': d.is_indexed,
                'created_at': d.created_at,
            }
            for d in docs
        ])
    
    elif request.method == 'POST':
        import asyncio
        
        name = request.data.get('name', 'Untitled')
        content = request.data.get('content', '')
        file_type = request.data.get('file_type', 'text')
        
        # Handle file upload
        if 'file' in request.FILES:
            file = request.FILES['file']
            name = file.name
            content = file.read().decode('utf-8', errors='ignore')
            file_type = name.split('.')[-1] if '.' in name else 'txt'
        
        doc = Document.objects.create(
            user=request.user,
            name=name,
            content=content,
            file_type=file_type,
            file_size=len(content),
        )
        
        # Index document
        async def index():
            kb = get_knowledge_base()
            await kb.initialize()
            chunks = await kb.add_document(doc.id, content, {'name': name})
            doc.chunk_count = len(chunks)
            doc.is_indexed = True
            doc.save()
        
        try:
            asyncio.run(index())
        except Exception as e:
            doc.indexing_error = str(e)
            doc.save()
        
        return Response({
            'id': doc.id,
            'name': doc.name,
            'chunk_count': doc.chunk_count,
            'is_indexed': doc.is_indexed,
        }, status=status.HTTP_201_CREATED)


@api_view(['GET', 'DELETE'])
@permission_classes([IsAuthenticated])
def document_detail(request, document_id: int):
    """
    GET: Get document details
    DELETE: Delete document
    """
    doc = get_object_or_404(Document, id=document_id, user=request.user)
    
    if request.method == 'GET':
        return Response({
            'id': doc.id,
            'name': doc.name,
            'content': doc.content,
            'file_type': doc.file_type,
            'file_size': doc.file_size,
            'chunk_count': doc.chunk_count,
            'is_indexed': doc.is_indexed,
            'metadata': doc.metadata,
            'created_at': doc.created_at,
            'updated_at': doc.updated_at,
        })
    
    elif request.method == 'DELETE':
        doc.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def rag_search(request):
    """
    Search documents using RAG.
    
    Request body:
        - query: Search query
        - top_k: Number of results (default: 5)
    """
    import asyncio
    
    query = request.data.get('query', '')
    top_k = int(request.data.get('top_k', 5))
    
    if not query:
        return Response({'error': 'Query is required'}, status=400)
    
    async def search():
        kb = get_knowledge_base()
        return await kb.search(query, top_k=top_k)
    
    results = asyncio.run(search())
    
    return Response({
        'query': query,
        'results': [
            {
                'document_id': r.document_id,
                'content': r.content,
                'score': r.score,
            }
            for r in results
        ]
    })


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def rag_query(request):
    """
    Ask a question using RAG pipeline.
    
    Request body:
        - question: The question to ask
        - llm_type: LLM to use (openai, gemini, ollama)
        - credential_id: LLM credential ID
        - top_k: Number of context chunks (default: 5)
    """
    import asyncio
    
    question = request.data.get('question', '')
    llm_type = request.data.get('llm_type', 'openai')
    credential_id = request.data.get('credential_id')
    top_k = int(request.data.get('top_k', 5))
    
    if not question:
        return Response({'error': 'Question is required'}, status=400)
    
    async def query():
        pipeline = get_rag_pipeline()
        await pipeline.kb.initialize()
        return await pipeline.query(
            question=question,
            user_id=request.user.id,
            llm_type=llm_type,
            top_k=top_k,
            credential_id=credential_id,
        )
    
    result = asyncio.run(query())
    
    return Response(result)
