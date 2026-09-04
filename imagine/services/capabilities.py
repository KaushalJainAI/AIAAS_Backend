"""Cached access to the model catalog.

The catalog is global — every user sees the same OpenRouter models — but
fetching it needs *a* credential, so the cache is keyed globally while the
fetch is performed with whichever user asked first. One shared accessor here
means the views, the intent classifier and the serializer validator cannot
drift on cache key or TTL, which they had already started to do.
"""
import logging

from django.core.cache import cache

from .catalog import EMPTY_CAPABILITIES, Capabilities
from .openrouter import MissingOpenRouterCredentialError, OpenRouterService

logger = logging.getLogger(__name__)

CACHE_KEY = "openrouter_capabilities_v2"
CACHE_TTL = 3600
#: Held while a `?refresh=1` fetch is in flight so concurrent refreshes do not
#: all hit OpenRouter at once; the losers serve the cached copy instead.
_REFRESH_LOCK_KEY = "openrouter_capabilities_refreshing"
_REFRESH_LOCK_TTL = 30


def _is_populated(caps: Capabilities) -> bool:
    return any(caps.get(k) for k in ("image", "video", "audio"))


def capabilities_for(user, *, refresh: bool = False) -> Capabilities:
    """Return the catalog, fetching and caching it on a miss.

    Returns empty buckets rather than raising when the user has no OpenRouter
    credential — callers that need to *tell* the user about that check for a
    missing credential explicitly (see `ImagineViewSet.capabilities`).

    `refresh=True` bypasses the cache. Refreshes are serialised by a short
    lock: the first caller fetches and repopulates the cache, the rest fall
    back to the cached copy rather than stampeding OpenRouter.
    """
    if not refresh:
        cached = cache.get(CACHE_KEY)
        if cached:
            return cached

    if refresh and cache.add(_REFRESH_LOCK_KEY, True, timeout=_REFRESH_LOCK_TTL):
        try:
            return _fetch_and_cache(user)
        finally:
            cache.delete(_REFRESH_LOCK_KEY)

    cached = cache.get(CACHE_KEY)
    if cached:
        return cached
    return _fetch_and_cache(user)


def _fetch_and_cache(user) -> Capabilities:
    try:
        service = OpenRouterService.for_user(user)
    except MissingOpenRouterCredentialError:
        return dict(EMPTY_CAPABILITIES)

    caps = service.fetch_models()
    # Never cache an empty catalog: a transient OpenRouter outage would
    # otherwise blank every model picker for a full hour.
    if _is_populated(caps):
        cache.set(CACHE_KEY, caps, CACHE_TTL)
    else:
        logger.warning("OpenRouter returned an empty capability catalog; not caching")
    return caps
