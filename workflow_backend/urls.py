"""
URL configuration for workflow_backend project.
"""
from django.contrib import admin
from django.urls import path, include
from django.http import JsonResponse
from orchestrator.views import receive_webhook


def health_check(request):
    """Health check endpoint for Docker/load balancers"""
    return JsonResponse({'status': 'healthy', 'service': 'workflow-backend'})


from core.auth_views import GoogleLogin

urlpatterns = [
    path('admin/', admin.site.urls),
    
    # Authentication
    path('api/auth/google/', GoogleLogin.as_view(), name='google_login'),
    path('api/auth/', include('dj_rest_auth.urls')),
    path('api/auth/registration/', include('dj_rest_auth.registration.urls')),
    
    # Health check
    path('api/health/', health_check, name='health-check'),
    
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
]


# Serve media files in development
from django.conf import settings
from django.conf.urls.static import static

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
    urlpatterns += static(settings.STATIC_URL, document_root=settings.STATIC_ROOT)

