"""
Custom Middleware for Workflow Backend

Includes:
- Input sanitization middleware for prompt injection prevention
- Rate limit header middleware
- Request logging middleware
"""
import json
import logging
import time
from typing import Optional

from asgiref.sync import iscoroutinefunction, markcoroutinefunction
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.utils.functional import SimpleLazyObject, empty

from core.safety.security import USER_NOTICE, get_sanitizer, SecurityViolation
from .throttling import get_throttle_headers

logger = logging.getLogger(__name__)



class HybridMiddleware:
    """Runs its hooks inline in whichever mode the handler is in.

    These three were `MiddlewareMixin` subclasses with sync hooks. Under daphne
    the handler chain is async, so Django adapted each one with
    `sync_to_async` — a hop onto the request's thread and back for every hook,
    on every request. None of them does I/O, so none of them needs a thread:
    this is Django's "both sync and async" middleware shape, and the hooks keep
    the `MiddlewareMixin` names and contract (a response from `process_request`
    short-circuits the view).

    The one thing that was only safe on a thread is `request.user`. It is a
    lazy object that reads the session store when first touched, which raises
    `SynchronousOnlyOperation` in an async context — so `process_response` is
    handed a user id resolved by `_user_id` / `_auser_id` instead.
    """

    sync_capable = True
    async_capable = True

    def __init__(self, get_response):
        self.get_response = get_response
        self.async_mode = iscoroutinefunction(get_response)
        if self.async_mode:
            markcoroutinefunction(self)

    def __call__(self, request):
        if self.async_mode:
            return self.__acall__(request)
        response = self.process_request(request)
        if response is None:
            response = self.get_response(request)
        return self.process_response(request, response, _user_id(request))

    async def __acall__(self, request):
        response = self.process_request(request)
        if response is None:
            response = await self.get_response(request)
        return self.process_response(request, response, await _auser_id(request))

    def process_request(self, request: HttpRequest) -> Optional[HttpResponse]:
        return None

    def process_response(self, request, response, user_id):
        return response


def _id_of(user) -> object:
    return user.id if user is not None and user.is_authenticated else 'anonymous'


def _user_id(request: HttpRequest) -> object:
    return _id_of(getattr(request, 'user', None))


async def _auser_id(request: HttpRequest) -> object:
    user = getattr(request, 'user', None)
    # DRF assigns the authenticated user onto the Django request, so after an
    # API view this is usually a plain model instance. Only a lazy object that
    # nothing has evaluated yet needs the session store, and that has to be
    # awaited rather than touched.
    if isinstance(user, SimpleLazyObject) and user._wrapped is empty:
        auser = getattr(request, 'auser', None)
        user = await auser() if auser is not None else None
    return _id_of(user)


#: Distinguishes "the body decoded to None" from "the body never decoded".
_UNPARSED = object()


def _charsets_to_try(declared: str) -> list[str]:
    """The declared charset first, then utf-8 — without trying utf-8 twice."""
    if declared.lower() == 'utf-8':
        return ['utf-8']
    return [declared, 'utf-8']


class InputSanitizationMiddleware(HybridMiddleware):
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

        # Get raw content type header for charset parsing
        raw_content_type = request.META.get('CONTENT_TYPE', '')
        if 'application/json' not in raw_content_type:
            return None

        # Parse charset
        charset = 'utf-8'
        if 'charset=' in raw_content_type:
            try:
                charset = raw_content_type.split('charset=')[-1].split(';')[0].strip()
            except Exception:
                pass

        # Try the declared charset, then utf-8. A body we cannot read is a body
        # we cannot sanitize, so we fail open and let the view (or DRF) reject
        # it — blocking here would turn every malformed request into a 400 from
        # a security middleware, which is not where that belongs.
        body = _UNPARSED
        for candidate in _charsets_to_try(charset):
            try:
                body = json.loads(request.body.decode(candidate))
                break
            except (json.JSONDecodeError, UnicodeDecodeError, LookupError):
                continue
        if body is _UNPARSED:
            logger.warning("Failed to decode JSON body with charset %s", charset)
            return None

        # Only object bodies have named fields to sanitize. A non-dict here
        # (a bare JSON scalar/list, or a body that came back malformed under a
        # concurrent ASGI request-body read) has nothing for us to inspect —
        # fail open and let the view/DRF parse it. Without this guard,
        # `field in body` raises TypeError and returns a 500 for the request
        # (observed: concurrent POSTs with a tiny `{}` body crashed here).
        if not isinstance(body, dict):
            return None

        # Check the relevant fields. The body is never rewritten: a message is
        # either refused whole or reaches the view exactly as typed.
        sanitizer = get_sanitizer()
        violations = []

        for field in self.SANITIZE_FIELDS:
            if field in body and isinstance(body[field], str):
                violations.extend(sanitizer.sanitize(body[field]).violations)

        blocked = [v for v in violations if v.action_taken == 'blocked']

        if blocked and self.BLOCK_ON_VIOLATION:
            # Refused before the view runs, so the message is never saved and
            # never enters the conversation history: the next message the user
            # sends is answered as if this one had not been typed. The client
            # reads `code` to take the message back out of the transcript.
            #
            # Logged in `process_response`, which is handed the user id: the
            # lazy `request.user` cannot be read here in async mode.
            request._blocked_violations = blocked
            return JsonResponse({
                'error': USER_NOTICE,
                'message': USER_NOTICE,
                'code': 'SECURITY_VIOLATION',
                'saved': False,
            }, status=400)

        # Store violations for logging
        if violations:
            request._security_violations = violations

        return None

    def process_response(
        self,
        request: HttpRequest,
        response: HttpResponse,
        user_id: object,
    ) -> HttpResponse:
        """Add security headers to response."""
        blocked = getattr(request, '_blocked_violations', None)
        if blocked:
            self._log_blocked_request(request, blocked, user_id)
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
        violations: list[SecurityViolation],
        user_id: object,
    ):
        """Log blocked request for security audit."""

        logger.warning(
            f"Blocked request due to security violations: "
            f"user={user_id}, path={request.path}, "
            f"violations={[v.pattern_name for v in violations]}"
        )


class RateLimitHeaderMiddleware(HybridMiddleware):
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
        response: HttpResponse,
        user_id: object,
    ) -> HttpResponse:
        """Add rate limit headers from throttle info."""
        # Check if throttle info is available
        if hasattr(request, '_throttle_info'):
            throttle = request._throttle_info
            headers = get_throttle_headers(throttle)
            for key, value in headers.items():
                response[key] = value

        return response


class RequestLoggingMiddleware(HybridMiddleware):
    """
    Middleware for logging API requests.

    Logs:
    - Request method, path, user
    - Response status code
    - Request duration
    """

    def process_request(self, request: HttpRequest) -> None:
        """Record request start time."""
        request._start_time = time.monotonic()
        return None

    def process_response(
        self,
        request: HttpRequest,
        response: HttpResponse,
        user_id: object,
    ) -> HttpResponse:
        """Log request completion."""
        duration = 0
        if hasattr(request, '_start_time'):
            duration = time.monotonic() - request._start_time

        # Log request (only for API endpoints)
        if request.path.startswith('/api/'):
            logger.info(
                f"{request.method} {request.path} "
                f"user={user_id} status={response.status_code} "
                f"duration={duration:.3f}s"
            )

        return response
