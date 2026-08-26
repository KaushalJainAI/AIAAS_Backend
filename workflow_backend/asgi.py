"""
ASGI config for workflow_backend project.

Exposes the ASGI callable with WebSocket support via Django Channels.

For more information on this file, see
https://docs.djangoproject.com/en/5.2/howto/deployment/asgi/
"""

import os

from django.core.asgi import get_asgi_application
from channels.routing import ProtocolTypeRouter, URLRouter

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'workflow_backend.settings.local')

# Initialize Django ASGI application early to populate AppRegistry
django_asgi_app = get_asgi_application()

# Import routing after Django setup
from streaming.routing import websocket_urlpatterns
from core.realtime.channels_middleware import JWTAuthMiddleware

# NOTE: nothing here validates the WebSocket Origin header. channels ships
# AllowedHostsOriginValidator for this and it was imported here at one point,
# but never wrapped around the route, so any page on any origin can open a
# socket. The practical risk is limited because these sockets authenticate
# from a ?token= query param rather than a cookie, so a hostile page has no
# way to obtain the victim's credential — this is not the classic cookie-auth
# hijack. Wiring the validator in is still the right hardening, but it keys
# off ALLOWED_HOSTS, which lists the *backend's* hostnames; the browser sends
# the *frontend's* origin. Enabling it without reconciling those two would cut
# off both the Vite dev server and production, so it needs a deliberate check
# against the deployed origins rather than a drive-by change.
application = ProtocolTypeRouter({
    # HTTP requests handled by Django
    "http": django_asgi_app,

    # WebSocket connections with authentication
    "websocket": JWTAuthMiddleware(
        URLRouter(websocket_urlpatterns)
    ),
})




