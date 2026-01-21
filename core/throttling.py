"""
Tier-Based Rate Limiting

Provides throttle classes that adjust rates based on user subscription tier.
Integrates with UserProfile to dynamically determine rate limits.

Usage:
    @throttle_classes([CompileThrottle])
    def compile_workflow(request):
        ...
"""
from rest_framework.throttling import UserRateThrottle, SimpleRateThrottle
from rest_framework.exceptions import Throttled
from django.core.cache import cache
import logging

logger = logging.getLogger(__name__)


class TierBasedThrottle(UserRateThrottle):
    """
    Base throttle that adjusts rates based on user subscription tier.
    
    Subclasses should define `tier_rates` dict mapping tier to rate string.
    Rate format: 'requests/period' (e.g., '10/minute', '100/hour')
    Use None for unlimited access.
    """
    
    # Override these in subclasses
    tier_rates = {
        'free': '100/hour',
        'pro': '1000/hour',
        'enterprise': None,  # Unlimited
    }
    
    # Fallback rate if tier not found
    default_rate = '50/hour'
    
    def get_rate(self):
        """Get rate based on user's tier."""
        if not hasattr(self, '_user_tier'):
            return self.default_rate
        
        tier = getattr(self, '_user_tier', 'free')
        rate = self.tier_rates.get(tier, self.default_rate)
        
        return rate
    
    def allow_request(self, request, view):
        """
        Check if request should be allowed based on user tier.
        
        Enterprise users with None rate get unlimited access.
        """
        # Get user tier from profile
        self._user_tier = 'free'  # Default
        
        if request.user and request.user.is_authenticated:
            try:
                profile = request.user.profile
                self._user_tier = profile.tier
            except AttributeError:
                pass
        
        # Check if tier has unlimited access
        if self.tier_rates.get(self._user_tier) is None:
            return True
        
        # Set rate dynamically
        self.rate = self.get_rate()
        self.num_requests, self.duration = self.parse_rate(self.rate)
        
        return super().allow_request(request, view)
    
    def get_cache_key(self, request, view):
        """Generate cache key including user ID."""
        if request.user and request.user.is_authenticated:
            ident = request.user.pk
        else:
            ident = self.get_ident(request)
        
        return f'throttle_{self.scope}_{ident}'


class CompileThrottle(TierBasedThrottle):
    """
    Rate limiting for workflow compilation endpoint.
    
    Limits:
        - Free: 10 compilations/minute
        - Pro: 100 compilations/minute
        - Enterprise: Unlimited
    """
    
    scope = 'compile'
    tier_rates = {
        'free': '10/minute',
        'pro': '100/minute',
        'enterprise': None,
    }


class ExecuteThrottle(TierBasedThrottle):
    """
    Rate limiting for workflow execution endpoint.
    
    Limits:
        - Free: 5 executions/minute
        - Pro: 50 executions/minute
        - Enterprise: 200 executions/minute
    """
    
    scope = 'execute'
    tier_rates = {
        'free': '5/minute',
        'pro': '50/minute',
        'enterprise': '200/minute',
    }


class StreamThrottle(TierBasedThrottle):
    """
    Rate limiting for streaming connections.
    
    This throttle limits the number of concurrent streaming connections
    rather than requests per time period.
    
    Limits:
        - Free: 5 concurrent connections
        - Pro: 20 concurrent connections
        - Enterprise: 100 concurrent connections
    """
    
    scope = 'stream'
    tier_rates = {
        'free': '5/minute',  # Used as connection limit
        'pro': '20/minute',
        'enterprise': '100/minute',
    }
    
    # Connection limits (separate from rate limiting)
    connection_limits = {
        'free': 5,
        'pro': 20,
        'enterprise': 100,
    }
    
    def allow_request(self, request, view):
        """
        Check both rate limits and connection limits.
        """
        # First check rate limit
        if not super().allow_request(request, view):
            return False
        
        # Then check concurrent connection limit
        return self._check_connection_limit(request)
    
    def _check_connection_limit(self, request):
        """Check if user has exceeded concurrent connection limit."""
        if not request.user or not request.user.is_authenticated:
            return True
        
        user_id = request.user.pk
        tier = getattr(self, '_user_tier', 'free')
        limit = self.connection_limits.get(tier, 5)
        
        # Get current connection count from cache
        cache_key = f'stream_connections_{user_id}'
        current = cache.get(cache_key, 0)
        
        if current >= limit:
            logger.warning(
                f"User {user_id} exceeded stream connection limit: {current}/{limit}"
            )
            return False
        
        return True
    
    @staticmethod
    def increment_connection(user_id: int):
        """Increment connection count when user connects."""
        cache_key = f'stream_connections_{user_id}'
        try:
            cache.incr(cache_key)
        except ValueError:
            cache.set(cache_key, 1, timeout=3600)  # 1 hour TTL
    
    @staticmethod
    def decrement_connection(user_id: int):
        """Decrement connection count when user disconnects."""
        cache_key = f'stream_connections_{user_id}'
        try:
            current = cache.get(cache_key, 0)
            if current > 0:
                cache.decr(cache_key)
        except ValueError:
            pass


class ChatThrottle(TierBasedThrottle):
    """
    Rate limiting for AI chat messages.
    
    Limits:
        - Free: 20 messages/hour
        - Pro: 200 messages/hour
        - Enterprise: 1000 messages/hour
    """
    
    scope = 'chat'
    tier_rates = {
        'free': '20/hour',
        'pro': '200/hour',
        'enterprise': '1000/hour',
    }


class LoginThrottle(SimpleRateThrottle):
    """
    Rate limiting for login attempts to prevent brute force.
    
    Applies to all users (authenticated and anonymous) based on IP.
    """
    
    scope = 'login'
    rate = '5/minute'
    
    def get_cache_key(self, request, view):
        """Use IP address as cache key for login attempts."""
        return f'throttle_login_{self.get_ident(request)}'


class RegistrationThrottle(SimpleRateThrottle):
    """
    Rate limiting for registration attempts.
    
    Prevents abuse of registration endpoint.
    """
    
    scope = 'register'
    rate = '3/minute'
    
    def get_cache_key(self, request, view):
        """Use IP address as cache key for registration attempts."""
        return f'throttle_register_{self.get_ident(request)}'


def get_throttle_headers(throttle_instance) -> dict:
    """
    Generate rate limit headers for response.
    
    Returns dict with X-RateLimit-* headers.
    """
    headers = {}
    
    if hasattr(throttle_instance, 'num_requests'):
        headers['X-RateLimit-Limit'] = str(throttle_instance.num_requests)
    
    if hasattr(throttle_instance, 'history'):
        remaining = throttle_instance.num_requests - len(throttle_instance.history)
        headers['X-RateLimit-Remaining'] = str(max(0, remaining))
    
    if hasattr(throttle_instance, 'wait'):
        wait = throttle_instance.wait()
        if wait:
            headers['X-RateLimit-Reset'] = str(int(wait))
    
    return headers
