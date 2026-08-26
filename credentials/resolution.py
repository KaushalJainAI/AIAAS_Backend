"""
One place that answers "which key do we use to call provider X for user Y?".

This used to live in `chat/llm.py` as private helpers, while `imagine` and the
intent classifier each grew their own lookup. The copies drifted on the part
that matters: chat required `is_verified=True`, the imagine copy did not, so a
credential the chat app refused to touch was used without complaint by media
generation. Divergence like that is not a behaviour difference anyone chose —
it is what happens when the same question is answered in three files.

The resolution order is deliberate and applies to every caller:

  1. the user's own credential (BYOK) — active, and verified unless the caller
     explicitly opts out
  2. a platform-managed key from the environment, so the product works before
     the user has configured anything
  3. nothing, which is an error the caller must handle

Callers that need a *credential id* (the node handlers, which decrypt inside
the execution context) use `resolve_credential_id`. Callers that need the key
material itself (direct HTTP clients like Imagine's media endpoints) use
`resolve_api_key`. Both share one definition of what "usable" means.
"""
from __future__ import annotations

import logging
import os

from asgiref.sync import sync_to_async

logger = logging.getLogger(__name__)


#: Providers whose credential-type slug differs from the provider slug.
#: Anything absent resolves to its own name, which is now every supported
#: provider — the entries that lived here belonged to providers retired in
#: favour of reaching their models through OpenRouter. Kept as a map rather
#: than deleted because the indirection is what lets a provider be added
#: without its credential type having to share the name.
CREDENTIAL_SLUGS: dict[str, tuple[str, ...]] = {}

#: Platform-managed fallback keys, read from the environment. Used only when the
#: user has no personal credential, so the app works out of the box.
#: NVIDIA is the one the product ships configured, so a user who has set up
#: nothing still gets a working assistant.
PLATFORM_ENV_KEYS: dict[str, tuple[str, ...]] = {
    'nvidia': ('NVIDIA_API_KEY',),
    'openrouter': ('OPENROUTER_API_KEY', 'OPEN_ROUTER_KEY'),
    'openai': ('OPENAI_API_KEY',),
}

#: Runs locally, needs no key.
KEYLESS_PROVIDERS = frozenset({'ollama'})

#: Field names an API key may be stored under. The seeded schemas use `apiKey`;
#: the others are accepted because credentials written by earlier revisions of
#: the credential UI are still in users' vaults.
_KEY_FIELDS = ('apiKey', 'api_key', 'token')


class CredentialUnavailable(RuntimeError):
    """No usable credential for a provider — neither user-owned nor platform.

    Raised rather than returned as a sentinel so a caller cannot mistake "no
    key" for "empty key" and send an unauthenticated request.
    """


def slugs_for(provider: str) -> tuple[str, ...]:
    """Credential-type slugs that satisfy `provider`."""
    return CREDENTIAL_SLUGS.get(provider, (provider,))


def platform_api_key(provider: str) -> str | None:
    """Platform default key for `provider` from the environment, if configured."""
    for env_name in PLATFORM_ENV_KEYS.get(provider, ()):  # first match wins
        if value := os.environ.get(env_name, "").strip():
            return value
    return None


def extract_api_key(data: dict | None) -> str | None:
    """Pull the key out of decrypted credential data, whatever field it used."""
    if not data:
        return None
    for field in _KEY_FIELDS:
        if value := (data.get(field) or "").strip():
            return value
    return None


# ── Credential id (for node handlers that decrypt downstream) ────────────────

def _lookup_credential(provider: str, user_id: int, *, require_verified: bool):
    from credentials.models import Credential

    filters = {
        'user_id': user_id,
        'credential_type__slug__in': slugs_for(provider),
        'is_active': True,
    }
    if require_verified:
        filters['is_verified'] = True
    return (
        Credential.objects
        .filter(**filters)
        .order_by('-updated_at')
        .first()
    )


def resolve_credential_id_sync(
    provider: str, user_id: int, *, require_verified: bool = True,
) -> str | None:
    """Id of the user's usable credential for `provider`, or None."""
    cred = _lookup_credential(provider, user_id, require_verified=require_verified)
    return str(cred.id) if cred else None


async def resolve_credential_id(
    provider: str, user_id: int, *, require_verified: bool = True,
) -> str | None:
    """Async form of `resolve_credential_id_sync`."""
    return await sync_to_async(resolve_credential_id_sync)(
        provider, user_id, require_verified=require_verified,
    )


# ── Key material (for callers that hold their own HTTP client) ───────────────

def resolve_api_key_sync(
    provider: str,
    user_id: int,
    *,
    require_verified: bool = True,
    allow_platform_fallback: bool = True,
) -> str:
    """The API key to call `provider` with, for this user.

    Raises `CredentialUnavailable` when there is none. `require_verified=False`
    is available for callers that genuinely accept an unverified key, but it is
    now an explicit decision at the call site rather than an omission.
    """
    if provider in KEYLESS_PROVIDERS:
        return ""

    cred = _lookup_credential(provider, user_id, require_verified=require_verified)
    if cred is not None:
        if key := extract_api_key(cred.get_credential_data()):
            return key
        logger.warning(
            "Credential %s for provider '%s' has no recognisable key field",
            cred.id, provider,
        )

    if allow_platform_fallback and (key := platform_api_key(provider)):
        return key

    raise CredentialUnavailable(
        f"No usable {provider} credential for this user. Add one under "
        f"Credentials, or configure a platform key."
    )


async def resolve_api_key(
    provider: str,
    user_id: int,
    *,
    require_verified: bool = True,
    allow_platform_fallback: bool = True,
) -> str:
    """Async form of `resolve_api_key_sync`."""
    return await sync_to_async(resolve_api_key_sync)(
        provider, user_id,
        require_verified=require_verified,
        allow_platform_fallback=allow_platform_fallback,
    )
