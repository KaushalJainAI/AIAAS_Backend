"""
WebSocket URL Routing

Defines WebSocket URL patterns for real-time features.
"""
from django.urls import re_path
from . import consumers
from buddy.consumers import BuddyConsumer
from canvas_agent.consumers import CanvasAgentConsumer

websocket_urlpatterns = [
    # Execution updates and HITL (per-execution)
    re_path(
        r'ws/execution/(?P<execution_id>[0-9a-f-]+)/$',
        consumers.ExecutionConsumer.as_asgi()
    ),
    
    # HITL notifications (per-user, all executions)
    re_path(
        r'ws/hitl/$',
        consumers.HITLNotificationConsumer.as_asgi()
    ),

    # Buddy (Help Assistant)
    re_path(
        r'ws/buddy/$',
        BuddyConsumer.as_asgi()
    ),
    
    # Canvas Agent
    re_path(
        r'ws/canvas-agent/$',
        CanvasAgentConsumer.as_asgi()
    ),
]
