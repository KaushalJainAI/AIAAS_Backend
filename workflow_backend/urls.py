"""
URL configuration for workflow_backend project.
"""
from django.contrib import admin
from django.urls import path, include
from django.http import HttpResponse, JsonResponse
from rest_framework.permissions import IsAdminUser
from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerView, SpectacularRedocView


def health_check(request):
    """Health check endpoint for Docker/load balancers"""
    return JsonResponse({'status': 'healthy', 'service': 'workflow-backend'})


def admin_login(request, extra_context=None):
    """The admin sign-in, with the same 5/minute limit as the API login.

    `/admin/` is proxied publicly, and DRF's throttles never reach Django's
    own admin view, so a superuser password could be guessed at full speed
    (N6, docs/SECURITY_REVIEW_FIX_PLAN.md). Only a POST is an attempt.
    """
    from core.views import LoginRateThrottle

    if request.method == 'POST' and not LoginRateThrottle().allow_request(request, None):
        return HttpResponse('Too many sign-in attempts. Wait a minute.', status=429)
    return admin.site.login(request, extra_context)


urlpatterns = [
    # Before `admin.site.urls`, so it wins the match (N6).
    path('admin/login/', admin_login, name='admin-login'),
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

    # Tool library — read-only catalogue of standard tools (grouped by grant)
    path('api/tools/', include('tools_config.urls')),

    # Custom tools — the user's own API and database connections (private rows)
    path('api/datasources/', include('datasources.urls')),

    # Standalone Chat
    path('api/chat/', include('chat.urls')),

    # Missions — the HTTP routes P7 left out (only the model could start one).
    path('api/missions/', include('missions.urls')),


    # Notifications
    path('api/notifications/', include('notifications.urls')),
    
    # Imagine (Image/Video/Audio Generation)
    path('api/imagine/', include('imagine.urls')),

    # Extract (document -> rows, owned by inference)
    path('api/extraction/', include('inference.extraction_urls')),

    # E-sign webhook (provider callbacks; the secret is the credential)
    path('api/esign/', include('esign.urls')),

    # Messaging webhooks (provider callbacks; channel + secret attribute)
    path('api/messaging/', include('messaging.urls')),

    # Eval (sub-agent evaluation + human supervision of the graders)
    path('api/eval/', include('eval.urls')),

    # Workspaces (compute plane: jobs wake the agent on exit)
    path('api/workspaces/', include('workspaces.urls')),
]


# Serve media files in development
from django.conf import settings
from django.conf.urls.static import static

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
    urlpatterns += static(settings.STATIC_URL, document_root=settings.STATIC_ROOT)

