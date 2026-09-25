"""
API Key Authentication Backend

Provides authentication via X-API-Key header for programmatic access.
"""
from rest_framework import authentication
from rest_framework.exceptions import AuthenticationFailed
from datetime import timedelta

from django.utils import timezone
from core.models import APIKey

#: How stale `last_used_at` may get before a request rewrites it.
TOUCH_SECONDS = 60


class APIKeyAuthentication(authentication.BaseAuthentication):
    """
    Authenticate requests via X-API-Key header.
    
    Usage:
        curl -H "X-API-Key: your-api-key" https://api.example.com/endpoint
    """
    
    keyword = 'X-API-Key'
    
    def authenticate(self, request):
        api_key = request.headers.get(self.keyword)
        
        if not api_key:
            return None  # No API key provided, try other auth methods
        
        try:
            key_obj = APIKey.objects.select_related('user', 'user__profile').get(
                key=APIKey.hash_key(api_key),  # only the hash is stored (S6)
                is_active=True
            )
        except APIKey.DoesNotExist:
            raise AuthenticationFailed('Invalid API key')
        
        now = timezone.now()
        if key_obj.expires_at and key_obj.expires_at < now:
            raise AuthenticationFailed('API key has expired')
        
        # `last_used_at` is for a person reading "used 3 minutes ago", so it
        # needs minute precision, not a write lock on every request (P1).
        if (key_obj.last_used_at is None
                or now - key_obj.last_used_at > timedelta(seconds=TOUCH_SECONDS)):
            APIKey.objects.filter(pk=key_obj.pk).update(last_used_at=now)
        
        return (key_obj.user, key_obj)
    
    def authenticate_header(self, request):
        return self.keyword
