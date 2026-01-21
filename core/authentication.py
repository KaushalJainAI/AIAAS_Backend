"""
API Key Authentication Backend

Provides authentication via X-API-Key header for programmatic access.
"""
from rest_framework import authentication
from rest_framework.exceptions import AuthenticationFailed
from django.utils import timezone
from .models import APIKey


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
                key=api_key,
                is_active=True
            )
        except APIKey.DoesNotExist:
            raise AuthenticationFailed('Invalid API key')
        
        # Check expiration
        if key_obj.expires_at and key_obj.expires_at < timezone.now():
            raise AuthenticationFailed('API key has expired')
        
        # Update last used timestamp
        key_obj.last_used_at = timezone.now()
        key_obj.save(update_fields=['last_used_at'])
        
        return (key_obj.user, key_obj)
    
    def authenticate_header(self, request):
        return self.keyword
