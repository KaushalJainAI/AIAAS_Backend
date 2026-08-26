"""
URL configuration for workflow_backend project.
"""
from django.contrib import admin
from django.urls import path, include
from django.http import JsonResponse
from rest_framework.permissions import IsAdminUser
from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerView, SpectacularRedocView


def health_check(request):
    """Health check endpoint for Docker/load balancers"""
    return JsonResponse({'status': 'healthy', 'service': 'workflow-backend'})


urlpatterns = [
    path('admin/', admin.site.urls),

    # Health check
    path('api/health/', health_check, name='health-check'),

    # API Schema & Docs — admin-only. Exposing the full API surface + schema
    # unauthenticated leaks the entire backend contract to anyone.
    path('api/schema/', SpectacularAPIView.as_view(permission_classes=[IsAdminUser]), name='schema'),
    path('api/schema/json/', SpectacularAPIView.as_view(permission_classes=[IsAdminUser]), name='schema-json'),
    path('api/docs/', SpectacularSwaggerView.as_view(url_name='schema', permission_classes=[IsAdminUser]), name='swagger-ui'),
    path('api/redoc/', SpectacularRedocView.as_view(url_name='schema', permission_classes=[IsAdminUser]), name='redoc'),

    # Aliases for common paths
    path('swagger.json', SpectacularAPIView.as_view(permission_classes=[IsAdminUser])),
    path('openapi.json', SpectacularAPIView.as_view(permission_classes=[IsAdminUser])),
    path('redoc/', SpectacularRedocView.as_view(url_name='schema', permission_classes=[IsAdminUser])),
    path('docs/', SpectacularSwaggerView.as_view(url_name='schema', permission_classes=[IsAdminUser])),
    
    # Core (auth, users, API keys)
    path('api/', include('core.urls')),
    
    # AI provider vocabulary + the model registry the picker reads.
    path('api/', include('llm.urls')),

    
    
    # Streaming (SSE, events)
    path('api/streaming/', include('streaming.urls')),
    
    # Orchestrator (workflows, executions, HITL, chat)
    path('api/orchestrator/', include('agents.urls')),
    
    # Logs (insights, audit, executions)
    path('api/logs/', include('logs.urls')),
    
    # Inference (documents, RAG)
    path('api/inference/', include('inference.urls')),

    # Credentials
    path('api/credentials/', include('credentials.urls')),
    
    # Templates
    
    
    # MCP
    path('api/mcp/', include('mcp_integration.urls')),

    # Skills
    path('api/', include('skills.urls')),
    
    # Standalone Chat
    path('api/chat/', include('chat.urls')),


    # Notifications
    path('api/notifications/', include('notifications.urls')),
    
    # Imagine (Image/Video/Audio Generation)
    path('api/imagine/', include('imagine.urls')),

    # Extract (document -> rows, owned by inference)
    path('api/extraction/', include('inference.extraction_urls')),

    # Eval (sub-agent evaluation + human supervision of the graders)
    path('api/eval/', include('eval.urls')),
]


# Serve media files in development
from django.conf import settings
from django.conf.urls.static import static

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
    urlpatterns += static(settings.STATIC_URL, document_root=settings.STATIC_ROOT)

