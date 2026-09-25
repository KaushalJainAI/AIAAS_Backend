"""
Ending every session a user has, by moving one timestamp.

A JWT is valid until it expires — 24 h for an access token, 30 d for a refresh
token — and nothing about changing a password touches one already issued. So a
stolen token outlived the password it was stolen under, which is the one
moment the owner is actively trying to lock someone out.

`UserProfile.tokens_valid_after` is the cutoff: a token whose `iat` (issued-at,
set by simplejwt on every token) is older is refused. Three doors check it —
`RevocableJWTAuthentication` (REST), `RevocableTokenRefreshSerializer` (the
refresh endpoint, or a revoked refresh token would simply mint fresh access
tokens) and the WebSocket middleware. A per-token blacklist would need a row
per outstanding token; one column per user is enough because the only
revocation anyone asks for is "all of them".

The cutoff is read on every authenticated request, so it is cached. `revoke`
writes the cache *after* the row, and a cache failure reads the row, so the
cache can only ever make a check slower, never make a revoked token pass.
"""
from __future__ import annotations

from django.core.cache import cache
from django.utils import timezone
from rest_framework_simplejwt.authentication import JWTAuthentication
from rest_framework_simplejwt.exceptions import AuthenticationFailed, InvalidToken
from rest_framework_simplejwt.serializers import TokenRefreshSerializer
from rest_framework_simplejwt.tokens import RefreshToken

_CACHE_TTL = 300
#: Cached for users with no cutoff too — they are almost everyone, and caching
#: only the hit would leave the common case paying a query per request.
_NONE = 0


def _key(user_id) -> str:
    return f'auth:tokens_valid_after:{user_id}'


def _cutoff(user_id) -> int:
    """The cutoff as a unix second (0 = none)."""
    try:
        cached = cache.get(_key(user_id))
    except Exception:  # noqa: BLE001 — a cache outage must not open the door
        cached = None
    if cached is not None:
        return cached
    from core.models import UserProfile

    value = (UserProfile.objects.filter(user_id=user_id)
             .values_list('tokens_valid_after', flat=True).first())
    stamp = int(value.timestamp()) if value else _NONE
    try:
        cache.set(_key(user_id), stamp, _CACHE_TTL)
    except Exception:  # noqa: BLE001
        pass
    return stamp


def is_revoked(user_id, payload: dict) -> bool:
    """True when this token was issued before the user's cutoff.

    Compared in whole seconds because `iat` is whole seconds: a token minted in
    the same second as the revocation (the fresh pair `revoke` callers hand
    back) must pass, so the test is strictly-older.
    """
    cutoff = _cutoff(user_id)
    if not cutoff:
        return False
    iat = payload.get('iat')
    if iat is None:
        return True  # predates simplejwt's iat claim; cannot prove it is newer
    return int(iat) < cutoff


def revoke(user) -> None:
    """Refuse every token issued to `user` before now."""
    from core.models import UserProfile

    now = timezone.now().replace(microsecond=0)
    profile, _ = UserProfile.objects.get_or_create(user=user)
    profile.tokens_valid_after = now
    profile.save(update_fields=['tokens_valid_after'])
    try:
        cache.set(_key(user.pk), int(now.timestamp()), _CACHE_TTL)
    except Exception:  # noqa: BLE001 — the row is the truth; the cache just expires
        cache.delete(_key(user.pk))


def fresh_pair(user) -> dict:
    """A new token pair, for the one session a revocation should keep."""
    refresh = RefreshToken.for_user(user)
    return {'access': str(refresh.access_token), 'refresh': str(refresh)}


class RevocableJWTAuthentication(JWTAuthentication):
    def get_user(self, validated_token):
        user = super().get_user(validated_token)
        if is_revoked(user.pk, validated_token.payload):
            raise InvalidToken('This session has been signed out. Please sign in again.')
        return user


class RevocableTokenRefreshSerializer(TokenRefreshSerializer):
    def validate(self, attrs):
        from rest_framework_simplejwt.settings import api_settings

        token = RefreshToken(attrs['refresh'])
        user_id = token.payload.get(api_settings.USER_ID_CLAIM)
        if user_id is not None and is_revoked(user_id, token.payload):
            raise AuthenticationFailed('This session has been signed out. Please sign in again.')
        return super().validate(attrs)
