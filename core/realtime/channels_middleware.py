from urllib.parse import parse_qs
from asgiref.sync import ThreadSensitiveContext, sync_to_async
from channels.db import database_sync_to_async
from django.contrib.auth.models import AnonymousUser
from django.db import close_old_connections
from rest_framework_simplejwt.tokens import AccessToken
from django.contrib.auth import get_user_model
import logging

logger = logging.getLogger(__name__)
User = get_user_model()

@database_sync_to_async
def get_user(user_id):
    try:
        return User.objects.get(id=user_id)
    except User.DoesNotExist:
        return AnonymousUser()

class JWTAuthMiddleware:
    """
    Custom middleware for Channels to authenticate users via JWT in the query string.
    Expects ?token=ACCESS_TOKEN
    """
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        # The JWT lookup is a DB read; without a context it lands on asgiref's
        # process-wide single thread with every other socket's (G6). The
        # handshake gets its own thread — the consumer base does the same for
        # the connection itself — and hands it back when the socket closes.
        async with ThreadSensitiveContext():
            try:
                await self._authenticate(scope)
                return await self.app(scope, receive, send)
            finally:
                try:
                    await sync_to_async(close_old_connections)()
                except Exception:  # noqa: BLE001 — disconnect must not fail
                    logger.exception("WebSocket middleware failed closing connections")

    async def _authenticate(self, scope) -> None:
        # Extract token from query string
        query_string = scope.get("query_string", b"").decode()
        query_params = parse_qs(query_string)
        token = query_params.get("token", [None])[0]

        if token:
            try:
                # Validate token
                access_token = AccessToken(token)
                user_id = access_token.payload.get("user_id")
                # Attach user to scope
                scope["user"] = await get_user(user_id)
                logger.info(f"WebSocket authenticated user: {scope['user']}")
            except Exception as e:
                logger.warning(f"WebSocket JWT authentication failed: {e}")
                scope["user"] = AnonymousUser()
        else:
            scope["user"] = AnonymousUser()
