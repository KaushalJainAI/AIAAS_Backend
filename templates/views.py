from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated, AllowAny
from rest_framework.response import Response

from .models import WorkflowTemplate
from .serializers import WorkflowTemplateSerializer, TemplateListItemSerializer
from .services import TemplateService

@api_view(['GET'])
@permission_classes([IsAuthenticated])
def template_list(request):
    """
    List available templates.
    """
    templates = WorkflowTemplate.objects.filter(status='production').order_by('-usage_count')
    serializer = TemplateListItemSerializer(templates, many=True)
    return Response(serializer.data)

@api_view(['GET'])
@permission_classes([IsAuthenticated])
def template_detail(request, pk):
    """
    Get template details.
    """
    template = get_object_or_404(WorkflowTemplate, pk=pk)
    serializer = WorkflowTemplateSerializer(template)
    return Response(serializer.data)

@api_view(['POST'])
@permission_classes([IsAuthenticated])
def template_search(request):
    """
    Semantic search for templates.
    """
    query = request.data.get('query', '')
    if not query:
        return Response({'error': 'Query required'}, status=400)
    
    import asyncio
    service = TemplateService()
    results = asyncio.run(service.search_templates(query))
    
    # Format results
    data = []
    for r in results:
        tmpl = r['template']
        item = TemplateListItemSerializer(tmpl).data
        item['score'] = r['score']
        data.append(item)
        
    return Response(data)

@api_view(['POST'])
@permission_classes([IsAuthenticated])
def create_from_workflow(request, workflow_id):
    """
    Convert a user workflow into a template (Publish).
    """
    service = TemplateService()
    template = service.publish_workflow_as_template(workflow_id)
    if template:
        serializer = WorkflowTemplateSerializer(template)
        return Response(serializer.data, status=status.HTTP_201_CREATED)
    return Response({'error': 'Failed to create template'}, status=400)
