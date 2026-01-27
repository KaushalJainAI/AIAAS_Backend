"""
Node Views

API endpoints for node registry and schema discovery.
"""
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated

from .handlers.registry import get_registry


class NodeSchemaListView(APIView):
    """
    List all available node schemas.
    
    GET /api/nodes/
    Returns all registered node schemas for the frontend palette.
    """
    permission_classes = [IsAuthenticated]
    
    def get(self, request):
        registry = get_registry()
        schemas = registry.get_all_schemas()
        
        return Response({
            'count': len(schemas),
            'nodes': schemas
        })


class NodeSchemaByCategory(APIView):
    """
    List node schemas grouped by category.
    
    GET /api/nodes/categories/
    Returns nodes organized by category for sidebar display.
    """
    permission_classes = [IsAuthenticated]
    
    def get(self, request):
        registry = get_registry()
        grouped = registry.get_schemas_by_category()
        
        return Response({
            'categories': grouped
        })


class NodeSchemaDetailView(APIView):
    """
    Get schema for a specific node type.
    
    GET /api/nodes/{node_type}/
    Returns detailed schema for a single node type.
    """
    permission_classes = [IsAuthenticated]
    
    def get(self, request, node_type):
        registry = get_registry()
        
        if not registry.has_handler(node_type):
            return Response(
                {'error': f'Unknown node type: {node_type}'},
                status=404
            )
        
        handler = registry.get_handler(node_type)
        schema = handler.get_schema()
        
        return Response(schema.model_dump(by_alias=True))
