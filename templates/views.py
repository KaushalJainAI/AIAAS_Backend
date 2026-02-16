from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.decorators import permission_classes
from adrf.decorators import api_view
from rest_framework.permissions import IsAuthenticated, AllowAny
from rest_framework.response import Response
from rest_framework.pagination import PageNumberPagination

from .models import WorkflowTemplate, WorkflowRating, WorkflowBookmark, TemplateComment
from asgiref.sync import sync_to_async
from .serializers import (
    WorkflowTemplateSerializer, 
    TemplateListItemSerializer,
    WorkflowRatingSerializer,
    TemplateCommentSerializer
)
from .services import TemplateService

class TemplatePagination(PageNumberPagination):
    page_size = 12
    page_size_query_param = 'page_size'
    max_page_size = 50

@api_view(['GET'])
@permission_classes([IsAuthenticated])
def template_list(request):
    """
    List available templates with filtering and pagination.
    """
    category = request.query_params.get('category')
    sort = request.query_params.get('sort', 'usage_count')
    min_rating = request.query_params.get('min_rating')
    
    queryset = WorkflowTemplate.objects.filter(status='production')
    
    if category:
        queryset = queryset.filter(category=category)
    if min_rating:
        queryset = queryset.filter(average_rating__gte=float(min_rating))
        
    # Sort mapping
    sort_options = {
        'rating': '-average_rating',
        'usage_count': '-usage_count',
        'newest': '-created_at',
        'trending': '-fork_count' # Simple trending for now
    }
    
    queryset = queryset.order_by(sort_options.get(sort, '-usage_count'))
    
    paginator = TemplatePagination()
    page = paginator.paginate_queryset(queryset, request)
    if page is not None:
        serializer = TemplateListItemSerializer(page, many=True, context={'request': request})
        return paginator.get_paginated_response(serializer.data)
        
    serializer = TemplateListItemSerializer(queryset, many=True, context={'request': request})
    return Response(serializer.data)

@api_view(['GET'])
@permission_classes([IsAuthenticated])
def template_detail(request, pk):
    """
    Get template details.
    """
    template = get_object_or_404(WorkflowTemplate, pk=pk)
    serializer = WorkflowTemplateSerializer(template, context={'request': request})
    return Response(serializer.data)

@api_view(['POST'])
@permission_classes([IsAuthenticated])
async def template_search(request):
    """
    Hybrid semantic + fuzzy search with pagination.
    """
    query = request.data.get('query', '')
    category = request.data.get('category')
    min_rating = request.data.get('min_rating')
    sort = request.data.get('sort', 'relevance')
    page = int(request.data.get('page', 1))
    page_size = int(request.data.get('page_size', 12))
    
    service = TemplateService()
    results = await service.hybrid_search(
        query=query,
        category=category,
        min_rating=min_rating,
        sort=sort,
        page=page,
        page_size=page_size
    )
    
    @sync_to_async
    def get_serialized_data(items):
        serializer = TemplateListItemSerializer(
            items, 
            many=True, 
            context={'request': request}
        )
        return serializer.data

    serialized_items = await get_serialized_data(results['items'])
    
    return Response({
        'results': serialized_items,
        'count': results['total'],
        'page': results['page'],
        'pages': results['pages']
    })

@api_view(['POST'])
@permission_classes([IsAuthenticated])
async def rate_template(request, pk):
    # aget for template
    template = await WorkflowTemplate.objects.aget(pk=pk)
    
    stars = request.data.get('stars')
    review = request.data.get('review', '')
    
    if not stars or not (1 <= int(stars) <= 5):
        return Response({'error': 'Stars must be between 1 and 5'}, status=400)
    
    @sync_to_async
    def update_rating():
        rating, created = WorkflowRating.objects.update_or_create(
            template=template,
            user=request.user,
            defaults={'stars': stars, 'review': review}
        )
        return rating
        
    rating = await update_rating()
    
    # Async update stats
    service = TemplateService()
    await service.recalculate_rating(template.id)
    
    @sync_to_async
    def get_serialized_data(instance):
        return WorkflowRatingSerializer(instance).data
        
    serialized_data = await get_serialized_data(rating)
    return Response(serialized_data)

@api_view(['GET'])
@permission_classes([AllowAny])
def template_ratings(request, pk):
    """List all ratings for a template."""
    ratings = WorkflowRating.objects.filter(template_id=pk).order_by('-created_at')
    serializer = WorkflowRatingSerializer(ratings, many=True)
    return Response(serializer.data)

@api_view(['POST'])
@permission_classes([IsAuthenticated])
def bookmark_template(request, pk):
    """Toggle bookmark for a template."""
    template = get_object_or_404(WorkflowTemplate, pk=pk)
    bookmark, created = WorkflowBookmark.objects.get_or_create(
        template=template,
        user=request.user
    )
    
    if not created:
        bookmark.delete()
        return Response({'bookmarked': False})
    
    return Response({'bookmarked': True})

@api_view(['GET', 'POST'])
@permission_classes([IsAuthenticated])
def template_comments(request, pk):
    """List or add comments."""
    template = get_object_or_404(WorkflowTemplate, pk=pk)
    
    if request.method == 'POST':
        text = request.data.get('text')
        parent_id = request.data.get('parent_id')
        
        if not text:
            return Response({'error': 'Comment text required'}, status=400)
            
        parent = None
        if parent_id:
            parent = get_object_or_404(TemplateComment, id=parent_id, template=template)
            
        comment = TemplateComment.objects.create(
            template=template,
            user=request.user,
            text=text,
            parent=parent
        )
        return Response(TemplateCommentSerializer(comment).data, status=201)
        
    comments = TemplateComment.objects.filter(template=template, parent=None)
    serializer = TemplateCommentSerializer(comments, many=True)
    return Response(serializer.data)

@api_view(['GET'])
@permission_classes([IsAuthenticated])
async def similar_templates(request, pk):
    """Recommendation: find vector-similar templates."""
    # Use sync_to_async for get_object_or_404 or aget
    template = await WorkflowTemplate.objects.aget(pk=pk, status='production')
    
    service = TemplateService()
    # Simple semantic search using template name
    results = await service.hybrid_search(
        query=template.name,
        page_size=5
    )
    
    # Filter out itself
    others = [item for item in results['items'] if item.id != template.id]
    
    @sync_to_async
    def get_serialized_data(items):
        serializer = TemplateListItemSerializer(items, many=True, context={'request': request})
        return serializer.data
        
    serialized_data = await get_serialized_data(others)
    return Response(serialized_data)

@api_view(['POST'])
@permission_classes([IsAuthenticated])
async def create_from_workflow(request, workflow_id):
    service = TemplateService()
    template = await service.publish_workflow_as_template(workflow_id)
    if template:
        @sync_to_async
        def get_serialized_data(instance):
            serializer = WorkflowTemplateSerializer(instance, context={'request': request})
            return serializer.data
            
        serialized_data = await get_serialized_data(template)
        return Response(serialized_data, status=status.HTTP_201_CREATED)
    return Response({'error': 'Failed to create template'}, status=400)
