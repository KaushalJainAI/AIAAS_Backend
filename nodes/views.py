"""
Node Views

API endpoints for node registry and schema discovery.
"""
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated

from .handlers.registry import get_registry
from .models import AIProvider, AIModel
from credentials.models import Credential
from django.views.decorators.cache import never_cache
from django.utils.decorators import method_decorator


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


@method_decorator(never_cache, name='get')
class AIModelListView(APIView):
    """
    List all available AI providers and their models.
    Also returns whether the user has verified credentials for each provider
    and computes dynamic availability for providers and models.
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
            
            # A provider is generally available if it has credentials (or is ollama)
            provider_available = has_creds
            
            models = provider.models.filter(is_active=True)
            
            model_data = []
            for m in models:
                # Model is available if its provider is fully available,
                # OR if the model is free and the provider isn't local.
                # All free cloud models are available by default via the platform routing
                # Paid models require user's verified credentials
                model_available = provider_available or (m.is_free and provider_slug != 'ollama')
                
                model_data.append({
                    'name': m.name,
                    'value': m.value,
                    'is_free': m.is_free,
                    'description': m.description,
                    'available': model_available,
                    'supports_text_input': m.supports_text_input,
                    'supports_text_generation': m.supports_text_generation,
                    'supports_image_input': m.supports_image_input,
                    'supports_image_generation': m.supports_image_generation,
                    'supports_audio_input': m.supports_audio_input,
                    'supports_audio_generation': m.supports_audio_generation,
                    'supports_video_input': m.supports_video_input,
                    'supports_video_generation': m.supports_video_generation,
                    'supports_numeric_input': m.supports_numeric_input,
                    'supports_numeric_generation': m.supports_numeric_generation,
                    'supports_time_series_input': m.supports_time_series_input,
                    'supports_time_series_generation': m.supports_time_series_generation,
                    'supports_document_input': m.supports_document_input,
                    'supports_document_generation': m.supports_document_generation,
                    'supports_tabular_input': m.supports_tabular_input,
                    'supports_tabular_generation': m.supports_tabular_generation,
                    'supports_structured_output': m.supports_structured_output,
                    'supports_tool_calling': m.supports_tool_calling,
                    'supports_embedding_generation': m.supports_embedding_generation,
                })
            
            data.append({
                'name': provider.name,
                'slug': provider.slug,
                'description': provider.description,
                'icon': provider.icon,
                'has_credentials': has_creds,
                'available': provider_available,
                'models': model_data
            })
            
        return Response({
            'providers': data
        })
