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

from workflow_backend.observability import init_error_reporting  # noqa: E402

init_error_reporting('web')

# Initialize Django ASGI application early to populate AppRegistry
django_asgi_app = get_asgi_application()

# Import routing after Django setup
from streaming.routing import websocket_urlpatterns
from core.realtime.channels_middleware import JWTAuthMiddleware


class _StartScheduler:
    """Start the in-process trigger scheduler on the first HTTP request.

    A thin wrapper, not middleware: it calls `ensure_started()` (idempotent —
    one flag, one setting check) and passes the scope through untouched.
    `background.spawn` needs a running loop, which is why this lives on the
    request path rather than at import time.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        from agents.scheduler import ensure_started
        ensure_started()
        return await self.app(scope, receive, send)
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
    # HTTP requests handled by Django. Daphne sends no ASGI `lifespan`
    # events, so the in-process trigger scheduler starts on the first
    # request instead — the Docker healthcheck hits `/api/health/` within
    # seconds of boot, and `ensure_started` is idempotent after that.
    "http": _StartScheduler(django_asgi_app),

    # WebSocket connections with authentication
    "websocket": JWTAuthMiddleware(
        URLRouter(websocket_urlpatterns)
    ),
})




