"""
JWT via query param — for browser-initiated GETs that cannot set Authorization.

`<img src>` and `<a href>` cannot send custom headers, so `GET
/api/inference/documents/<id>/download/` as an `src` would always be 401 even
though the same user can fetch it via `apiClient.get(..., {headers: {Authorization}})`.


This authenticator mirrors `channels_middleware.JWTAuthMiddleware` (which reads
`?token=` for WebSockets) so the two transports accept the same credential
shape. It is deliberately *scoped* to safe methods and to the `token`
param only — POST/PUT/DELETE still require a header, and the token is not
read from `access_token` or any other alias that would widen the surface.
"""
from rest_framework_simplejwt.authentication import JWTAuthentication
from rest_framework_simplejwt.exceptions import InvalidToken


class QueryParamJWTAuthentication(JWTAuthentication):
    def authenticate(self, request):
        # Header-based auth already succeeded? Use it.
        header = self.get_header(request)
        if header is not None:
            return super().authenticate(request)

        # No header — try ?token= for safe, idempotent reads only.
        # `request` is a DRF Request; query_params is the parsed query string.
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            return None

        raw_token = request.query_params.get("token")
        if not raw_token:
            return None

        try:
            validated = self.get_validated_token(raw_token)
            user = self.get_user(validated)
            return (user, validated)
        except InvalidToken:
            # Let the view return 401 rather than 500 — the permission layer
            # will turn an unauthenticated request into 401.
            return None
        except Exception:
            return None
