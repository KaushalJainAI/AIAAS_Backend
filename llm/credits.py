"""
Credits: the allowance a user spends when a call runs on the *platform's* key.

`UserProfile.credits_remaining` existed since the first migration, was shown on
the Settings page, and was never read or written by anything on the model path
— `core.auth.permissions.HasCredits` was declared and attached to no view. So
every account could spend without limit on the key the platform pays for,
which is the one bill nobody watching the product sees coming.

Three rules, each deliberate:

- **Only the platform key is metered.** A call made with the user's own
  credential costs us nothing, and refusing it would punish exactly the users
  who did the work of bringing a key.
- **Free models are not metered.** The shipped default is OpenRouter's free
  router; charging credits for it would drain a new account's allowance on
  calls that cost nobody anything.
- **Checked before, charged after.** `access.preflight` refuses a turn with
  no credit left while the screen is still empty (the fail-fast rule), and the
  funnel charges once the provider has reported usage. A balance can therefore
  dip below what one more call costs, and is floored at zero rather than going
  negative: the cap is a blast-radius control, not an invoice.

Everything here degrades to *allowing* the call. A cost control that fails
closed on a database hiccup takes the whole product down with it, which is a
worse outcome than one extra call on the platform key.
"""
from __future__ import annotations

import logging
import math

from asgiref.sync import sync_to_async
from django.core.cache import cache

from workflow_backend.thresholds import TOKENS_PER_CREDIT

logger = logging.getLogger(__name__)

#: How long "is this model free" is remembered. The catalogue changes when the
#: seed script runs, not per request.
FREE_MODEL_CACHE_TTL = 300


def credits_for(tokens: int) -> int:
    """Credits one call costs. Rounded up so no metered call is free."""
    if tokens <= 0:
        return 0
    return math.ceil(tokens / TOKENS_PER_CREDIT)


def _free_cache_key(model: str) -> str:
    return f"llm:credits:is_free:{model}"


def _is_free_sync(model: str) -> bool:
    from llm.models import AIModel

    return AIModel.objects.filter(value=model, is_free=True).exists()


async def is_free_model(model: str) -> bool:
    """Whether `model` is marked free in the catalogue. Unknown means metered."""
    key = _free_cache_key(model)
    try:
        cached = cache.get(key)
    except Exception:  # noqa: BLE001 — a cache outage must not block a call
        cached = None
    if cached is not None:
        return bool(cached)
    try:
        free = await sync_to_async(_is_free_sync)(model)
    except Exception:  # noqa: BLE001
        logger.warning("[Credits] could not read is_free for %s", model, exc_info=True)
        return False
    try:
        cache.set(key, free, FREE_MODEL_CACHE_TTL)
    except Exception:  # noqa: BLE001
        pass
    return free


def _remaining_sync(user_id: int) -> int | None:
    from core.models import UserProfile

    return (
        UserProfile.objects.filter(user_id=user_id)
        .values_list("credits_remaining", flat=True)
        .first()
    )


async def has_credit(*, user_id: int, model: str) -> bool:
    """
    May this user start a platform-key call to `model`?

    True for free models, for users with no profile row (nothing to meter
    against), and whenever the balance cannot be read.
    """
    if await is_free_model(model):
        return True
    try:
        remaining = await sync_to_async(_remaining_sync)(user_id)
    except Exception:  # noqa: BLE001
        logger.warning("[Credits] could not read balance for user %s", user_id, exc_info=True)
        return True
    return remaining is None or remaining > 0


def _charge_sync(user_id: int, amount: int) -> None:
    from django.db.models import F, Value
    from django.db.models.functions import Greatest

    from core.models import UserProfile

    # One UPDATE, no read-modify-write: concurrent calls from a fan-out must
    # each subtract, not overwrite each other's result.
    UserProfile.objects.filter(user_id=user_id).update(
        credits_remaining=Greatest(F("credits_remaining") - amount, Value(0)),
        credits_used_total=F("credits_used_total") + amount,
    )


async def charge(*, user_id: int, model: str, tokens: int) -> int:
    """Deduct what a finished platform-key call used. Returns credits charged."""
    amount = credits_for(tokens)
    if amount == 0 or await is_free_model(model):
        return 0
    try:
        await sync_to_async(_charge_sync)(user_id, amount)
    except Exception:  # noqa: BLE001 — the answer is already produced
        logger.warning(
            "[Credits] could not charge %s credits to user %s", amount, user_id,
            exc_info=True,
        )
        return 0
    return amount
