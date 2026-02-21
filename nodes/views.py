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
from .models import AIProvider, AIModel
from credentials.models import Credential


class AIModelListView(APIView):
    """
    List all available AI providers and their models.
    Also returns whether the user has verified credentials for each provider.
    """
    permission_classes = [IsAuthenticated]
    
    def get(self, request):
        providers = AIProvider.objects.filter(is_active=True).prefetch_related('models')
        
        # Get user's verified credentials
        verified_type_slugs = set(
            Credential.objects.filter(
                user=request.user, 
                is_active=True, 
                is_verified=True
            ).values_list('credential_type__slug', flat=True)
        )
        
        data = []
        for provider in providers:
            # Map provider slug to credential type slug
            provider_slug = provider.slug
            
            # Default check
            has_creds = provider_slug in verified_type_slugs
            
            # Special manual mappings
            if provider_slug == 'ollama':
                has_creds = True
            elif provider_slug == 'gemini':
                has_creds = 'gemini-api' in verified_type_slugs or 'google-oauth2' in verified_type_slugs
            elif provider_slug == 'perplexity':
                has_creds = 'perplexity-api' in verified_type_slugs
            
            models = provider.models.filter(is_active=True)
            
            data.append({
                'name': provider.name,
                'slug': provider.slug,
                'description': provider.description,
                'icon': provider.icon,
                'has_credentials': has_creds,
                'models': [
                    {
                        'name': m.name,
                        'value': m.value,
                        'is_free': m.is_free,
                        'description': m.description,
                    } for m in models
                ]
            })
            
        return Response({
            'providers': data
        })
