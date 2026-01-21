"""
Custom Middleware for Workflow Backend

Includes:
- Input sanitization middleware for prompt injection prevention
- Rate limit header middleware
- Request logging middleware
"""
import json
import logging
from typing import Callable, Optional

from django.http import HttpRequest, HttpResponse, JsonResponse
from django.utils.deprecation import MiddlewareMixin

from .security import get_sanitizer, SecurityViolation
from .throttling import get_throttle_headers

logger = logging.getLogger(__name__)


class InputSanitizationMiddleware(MiddlewareMixin):
    """
    Middleware to sanitize request bodies before processing.
    
    Only applies to specific endpoints that handle user-generated content
    that will be sent to LLMs.
    
    Configuration:
        SANITIZE_ENDPOINTS: List of URL prefixes to sanitize
        SANITIZE_FIELDS: Fields to sanitize in JSON body
        BLOCK_ON_VIOLATION: Whether to block requests with violations
    """
    
    # Endpoints that need sanitization
    SANITIZE_ENDPOINTS = [
        '/api/chat/',
        '/api/orchestrator/',
        '/api/compile/',
        '/api/execute/',
    ]
    
    # Fields to check in JSON body
    SANITIZE_FIELDS = [
        'message',
        'content',
        'prompt',
        'query',
        'input',
        'text',
        'instruction',
    ]
    
    # Whether to block requests with critical violations
    BLOCK_ON_VIOLATION = True
    
    def process_request(self, request: HttpRequest) -> Optional[HttpResponse]:
        """
        Sanitize request body before view processing.
        
        Returns None to continue processing, or HttpResponse to block.
        """
        # Only check POST/PUT/PATCH with JSON body
        if request.method not in ('POST', 'PUT', 'PATCH'):
            return None
        
        # Check if endpoint needs sanitization
        if not self._should_sanitize(request.path):
            return None
        
        # Check content type
        content_type = request.content_type or ''
        if 'application/json' not in content_type:
            return None
        
        try:
            body = json.loads(request.body.decode('utf-8'))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return None  # Let view handle invalid JSON
        
        # Sanitize relevant fields
        sanitizer = get_sanitizer()
        violations = []
        modified = False
        
        for field in self.SANITIZE_FIELDS:
            if field in body and isinstance(body[field], str):
                result = sanitizer.sanitize(body[field])
                
                if result.violations:
                    violations.extend(result.violations)
                
                if result.was_modified:
                    body[field] = result.sanitized_text
                    modified = True
        
        # Check for critical violations
        critical_violations = [
            v for v in violations 
            if v.severity == 'critical' and v.action_taken == 'blocked'
        ]
        
        if critical_violations and self.BLOCK_ON_VIOLATION:
            self._log_blocked_request(request, critical_violations)
            return JsonResponse({
                'error': 'Request blocked due to security policy violation',
                'code': 'SECURITY_VIOLATION',
                'details': 'Input contains prohibited content patterns'
            }, status=400)
        
        # Store sanitized body for view
        if modified:
            request._sanitized_body = json.dumps(body).encode('utf-8')
            request._body = request._sanitized_body
        
        # Store violations for logging
        if violations:
            request._security_violations = violations
        
        return None
    
    def process_response(
        self, 
        request: HttpRequest, 
        response: HttpResponse
    ) -> HttpResponse:
        """Add security headers to response."""
        # Add security violation info to response headers (for debugging)
        if hasattr(request, '_security_violations'):
            violation_count = len(request._security_violations)
            response['X-Security-Violations'] = str(violation_count)
        
        return response
    
    def _should_sanitize(self, path: str) -> bool:
        """Check if path should have input sanitization."""
        return any(path.startswith(ep) for ep in self.SANITIZE_ENDPOINTS)
    
    def _log_blocked_request(
        self, 
        request: HttpRequest, 
        violations: list[SecurityViolation]
    ):
        """Log blocked request for security audit."""
        user = getattr(request, 'user', None)
        user_id = user.id if user and user.is_authenticated else 'anonymous'
        
        logger.warning(
            f"Blocked request due to security violations: "
            f"user={user_id}, path={request.path}, "
            f"violations={[v.pattern_name for v in violations]}"
        )


class RateLimitHeaderMiddleware(MiddlewareMixin):
    """
    Middleware to add rate limit headers to responses.
    
    Headers added:
    - X-RateLimit-Limit: Maximum requests allowed
    - X-RateLimit-Remaining: Requests remaining
    - X-RateLimit-Reset: Seconds until limit resets
    """
    
    def process_response(
        self, 
        request: HttpRequest, 
        response: HttpResponse
    ) -> HttpResponse:
        """Add rate limit headers from throttle info."""
        # Check if throttle info is available
        if hasattr(request, '_throttle_info'):
            throttle = request._throttle_info
            headers = get_throttle_headers(throttle)
            for key, value in headers.items():
                response[key] = value
        
        return response


class RequestLoggingMiddleware(MiddlewareMixin):
    """
    Middleware for logging API requests.
    
    Logs:
    - Request method, path, user
    - Response status code
    - Request duration
    """
    
    def process_request(self, request: HttpRequest) -> None:
        """Record request start time."""
        import time
        request._start_time = time.time()
    
    def process_response(
        self, 
        request: HttpRequest, 
        response: HttpResponse
    ) -> HttpResponse:
        """Log request completion."""
        import time
        
        # Calculate duration
        duration = 0
        if hasattr(request, '_start_time'):
            duration = time.time() - request._start_time
        
        # Get user info
        user = getattr(request, 'user', None)
        user_id = user.id if user and user.is_authenticated else 'anonymous'
        
        # Log request (only for API endpoints)
        if request.path.startswith('/api/'):
            logger.info(
                f"{request.method} {request.path} "
                f"user={user_id} status={response.status_code} "
                f"duration={duration:.3f}s"
            )
        
        return response
