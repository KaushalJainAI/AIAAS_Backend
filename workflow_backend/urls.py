"""
URL configuration for workflow_backend project.
"""
from django.contrib import admin
from django.urls import path, include
from django.http import JsonResponse
from orchestrator.views import receive_webhook
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
    
    # Nodes (node registry, schemas)
    path('api/', include('nodes.urls')),
    
    # Compiler (workflow compile/validate)
    path('api/', include('compiler.urls')),
    
    # Streaming (SSE, events)
    path('api/streaming/', include('streaming.urls')),
    
    # Orchestrator (workflows, executions, HITL, chat)
    path('api/orchestrator/', include('orchestrator.urls')),
    
    # Logs (insights, audit, executions)
    path('api/logs/', include('logs.urls')),
    
    # Inference (documents, RAG)
    path('api/inference/', include('inference.urls')),

    # Credentials
    path('api/credentials/', include('credentials.urls')),
    
    # Templates
    path('api/orchestrator/templates/', include('templates.urls')),
    
    # Webhooks (Public)
    path('api/webhooks/<int:user_id>/<path:webhook_path>', receive_webhook, name='webhook_receiver'),
    
    # MCP
    path('api/mcp/', include('mcp_integration.urls')),

    # Skills
    path('api/', include('skills.urls')),
    
    # Standalone Chat
    path('api/chat/', include('chat.urls')),
    
    # Buddy (Help Assistant)
    path('api/buddy/', include('buddy.urls')),

    # BrowserOS
    path('api/browseros/', include('browserOS.urls')),

    # Canvas Agent — DISABLED, see canvas_agent/ (app also commented out of INSTALLED_APPS)
    # path('api/canvas-agent/', include('canvas_agent.urls')),

    # Notifications
    path('api/notifications/', include('notifications.urls')),
    
    # Imagine (Image/Video/Audio Generation)
    path('api/imagine/', include('imagine.urls')),

    # Datasets (training and eval examples)
    path('api/', include('datasets.urls')),

    # MVP: Evals and Tuning are routed out until their executors exist.
    # Both apps accept work and record it as 'queued', but nothing consumes
    # that queue — `POST /evals/suites/{id}/run/` never scores, and a TuningJob
    # never leaves 'queued', which also makes `deploy` (completed-only)
    # unreachable. Serving endpoints that silently accept work they will never
    # do is worse than not serving them. The apps stay in INSTALLED_APPS so
    # their models and migrations are untouched; restore these two lines with
    # the workers.
    # path('api/evals/', include('evals.urls')),
    # path('api/tuning/', include('tuning.urls')),

    # Extract (document -> rows)
    path('api/extraction/', include('extraction.urls')),
]


# Serve media files in development
from django.conf import settings
from django.conf.urls.static import static

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
    urlpatterns += static(settings.STATIC_URL, document_root=settings.STATIC_ROOT)

