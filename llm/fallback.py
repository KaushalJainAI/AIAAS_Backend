"""
The platform fallback model: what a run executes on when its configured model
is retired or unknown, and how that substitution is decided.

The fallback is one row (`llm.models.ModelFallback`), edited by staff through
`/api/llm/fallback/` or Django admin — a row and not an env var, so changing
it needs no rebuild and no restart. Reads go through `get_fallback`, cached
for 60s (the witness-cache rule: a TTL is the whole bound, and a cache failure
resolves to the default rather than refusing).

`resolve_with_fallback` is pure and synchronous so every caller — agent
runtime, chat pipeline, serializer — applies the same rule: a configured
(retired) model is substituted *before* preflight, never rewritten in place.
The stored configuration keeps what the owner chose; the run record
(`ExecutionLog.model_used` / `fallback_from`) says what the run did about it.
"""
from __future__ import annotations

import logging

from django.core.cache import cache

logger = logging.getLogger(__name__)

#: What an unset fallback means. A router id on purpose: which concrete model
#: answers is chosen upstream per request, so this default survives individual
#: model retirements — the same reason new chats start on `openrouter/free`.
DEFAULT_FALLBACK_PROVIDER = 'openrouter'
DEFAULT_FALLBACK_MODEL = 'openrouter/free'

#: 60s, matching `tools_config.overlay` and the witness cache: there is no
#: invalidation hook here, so the TTL is the whole bound on staleness after a
#: staff edit — and the flip side is that no restart is ever needed.
FALLBACK_CACHE_KEY = 'llm_model_fallback'
FALLBACK_CACHE_TTL = 60


def get_fallback() -> tuple[str, str]:
    """(provider, model) the platform falls back to right now."""
    try:
        cached = cache.get(FALLBACK_CACHE_KEY)
    except Exception:  # noqa: BLE001 — losing the fast path must not break runs
        cached = None
    if cached:
        return cached[0], cached[1]
    pair = (DEFAULT_FALLBACK_PROVIDER, DEFAULT_FALLBACK_MODEL)
    try:
        from .models import ModelFallback

        row = ModelFallback.objects.order_by('pk').first()
        if row is not None and (row.model or '').strip():
            pair = ((row.provider or '').strip() or DEFAULT_FALLBACK_PROVIDER,
                    row.model.strip())
        cache.set(FALLBACK_CACHE_KEY, list(pair), FALLBACK_CACHE_TTL)
    except Exception:  # noqa: BLE001
        logger.warning('[Fallback] Could not read fallback row; using default',
                       exc_info=True)
    return pair


def set_fallback(provider: str, model: str, *, updated_by=None):
    """Store a new platform fallback. Returns (row, warning)."""
    from .models import AIModel, ModelFallback

    provider = (provider or '').strip() or DEFAULT_FALLBACK_PROVIDER
    model = (model or '').strip()
    warning = ''
    if model:
        row = AIModel.objects.filter(
            provider__slug=provider, value=model).first()
        if row is None:
            warning = (
                f'"{model}" is not in the {provider} catalogue we hold — '
                'saved anyway, and runs naming it will use it verbatim.'
            )
        elif not row.is_active:
            warning = (
                f'"{model}" is retired in our catalogue — saved anyway, but '
                'consider picking an active model as the fallback.'
            )
    obj, _ = ModelFallback.objects.update_or_create(
        pk=1, defaults={'provider': provider, 'model': model,
                        'updated_by': updated_by},
    )
    try:
        cache.delete(FALLBACK_CACHE_KEY)
    except Exception:  # noqa: BLE001
        pass
    return obj, warning


def resolve_with_fallback(provider: str, model: str,
                           ) -> tuple[str, str, bool, str]:
    """Apply the fallback rule to one configured (provider, model) pair.

    Returns (provider, model, substituted, reason). Never raises: a resolver
    failure returns the input unchanged, because refusing to run over a
    bookkeeping error is worse than running on the configured model.
    """
    from .models import AIModel

    provider = (provider or '').strip() or DEFAULT_FALLBACK_PROVIDER
    model = (model or '').strip()
    if not model:
        # Blank means "account default" upstream of here; nothing to judge.
        return provider, model, False, ''
    try:
        fallback = get_fallback()
    except Exception:  # noqa: BLE001
        return provider, model, False, ''
    if (provider, model) == (fallback[0], fallback[1]):
        return provider, model, False, ''
    try:
        rows = AIModel.objects.filter(provider__slug=provider)
        if not rows.exists():
            # No catalogue held (fresh install, or a provider we do not seed):
            # refusing every model would make the product unusable until
            # someone seeds it — the same rule `_model_problem` applies.
            return provider, model, False, ''
        if rows.filter(value=model, is_active=True).exists():
            return provider, model, False, ''
        retired = rows.filter(value=model).exists()
        reason = (
            f'"{model}" has been retired'
            if retired else
            f'"{model}" is not a {provider} model we hold'
        )
        return fallback[0], fallback[1], True, reason
    except Exception:  # noqa: BLE001
        logger.warning('[Fallback] Resolver failed for %s/%s', provider, model,
                       exc_info=True)
        return provider, model, False, ''
