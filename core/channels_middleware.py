from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser
from channels.db import database_sync_to_async
from channels.middleware import BaseMiddleware
from rest_framework_simplejwt.tokens import AccessToken
from rest_framework_simplejwt.exceptions import InvalidToken, TokenError
from urllib.parse import parse_qs
import logging

logger = logging.getLogger(__name__)

@database_sync_to_async
def get_user(token_key):
    try:
        token = AccessToken(token_key)
        user_id = token['user_id']
        User = get_user_model()
        return User.objects.get(id=user_id)
    except (InvalidToken, TokenError, User.DoesNotExist) as e:
        logger.warning(f"WebSocket JWT Auth Failed: {e}")
        return AnonymousUser()
    except Exception as e:
        logger.error(f"Unexpected error in WebSocket JWT Auth: {e}")
        return AnonymousUser()

class JWTAuthMiddleware(BaseMiddleware):
    async def __call__(self, scope, receive, send):
        # Parse query string for token
        query_string = scope.get('query_string', b'').decode()
        query_params = parse_qs(query_string)
        token = query_params.get('token', [None])[0]
        
        if token:
            scope['user'] = await get_user(token)
        else:
            if 'user' not in scope:
                scope['user'] = AnonymousUser()
        
        return await super().__call__(scope, receive, send)
