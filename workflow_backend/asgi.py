"""
ASGI config for workflow_backend project.

Exposes the ASGI callable with WebSocket support via Django Channels.

For more information on this file, see
https://docs.djangoproject.com/en/5.2/howto/deployment/asgi/
"""

import os

from django.core.asgi import get_asgi_application
from channels.routing import ProtocolTypeRouter, URLRouter
from channels.auth import AuthMiddlewareStack
from channels.security.websocket import AllowedHostsOriginValidator

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'workflow_backend.settings')

# Initialize Django ASGI application early to populate AppRegistry
django_asgi_app = get_asgi_application()

# Import routing after Django setup
from streaming.routing import websocket_urlpatterns
from core.channels_middleware import JWTAuthMiddleware

application = ProtocolTypeRouter({
    # HTTP requests handled by Django
    "http": django_asgi_app,
    
    # WebSocket connections with authentication
    "websocket": AllowedHostsOriginValidator(
        AuthMiddlewareStack(
            JWTAuthMiddleware(
                URLRouter(websocket_urlpatterns)
            )
        )
    ),
})

