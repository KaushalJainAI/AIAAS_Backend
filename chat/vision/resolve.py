"""
Who does the seeing, and on whose key.

Resolution is a *chain* rather than a single model because of a measured fact
about NIM: `/v1/models` is a catalog, not an entitlement list. Five of the models
it advertised for the platform key returned `404 Function ... not found for
account`. A single configured model is therefore one 404 away from a user having
no eyes at all, so a 404 must fall through to the next candidate instead of
failing the look.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from asgiref.sync import sync_to_async

logger = logging.getLogger(__name__)

#: The platform default and its cheaper fallback, in order. Both measured
#: correct against a real chart on 2026-08-13; the 12B reads more like a witness
#: and the 8B is terser but half the price. See docs/VISION_AGENT.md.
DEFAULT_PROVIDER = "nvidia"
DEFAULT_CHAIN: tuple[str, ...] = (
    "nvidia/nemotron-nano-12b-v2-vl",
    "nvidia/llama-3.1-nemotron-nano-vl-8b-v1",
)

#: The OCR/layout specialist used only to cross-check the witness on glyphs.
#: Never the witness itself: it answers no questions, it transcribes.
PARSE_MODEL = "nvidia/nemotron-parse"


@dataclass(frozen=True, slots=True)
class Witness:
    """A vision model that is callable for this user, in preference order."""

    provider: str
    #: Ordered candidates. The first is tried; a 404 falls through to the next.
    models: tuple[str, ...]

    @property
    def model(self) -> str:
        return self.models[0]


@sync_to_async
def _configured(user_id: int) -> tuple[str, str]:
    """The user's chosen (provider, model), or ('', '') if they have no profile."""
    from core.models import UserProfile

    row = (
        UserProfile.objects.filter(user_id=user_id)
        .values("vision_provider", "vision_model")
        .first()
    )
    if not row:
        return "", ""
    return (row["vision_provider"] or "").strip(), (row["vision_model"] or "").strip()


def _has_key(provider: str, user_id: int) -> bool:
    """Whether *any* key exists for `provider` — user credential or platform."""
    from credentials.resolution import CredentialUnavailable, resolve_api_key_sync

    try:
        return bool(
            resolve_api_key_sync(provider, user_id, require_verified=False)
        )
    except CredentialUnavailable:
        return False
    except Exception:  # noqa: BLE001
        # Credential lookup is not the feature. If it breaks, the honest answer
        # is "no witness", which degrades to today's behaviour rather than a 500
        # in the middle of someone's chat turn.
        logger.warning("[Vision] Credential lookup failed for %s", provider,
                       exc_info=True)
        return False


async def resolve_witness(user_id: int) -> Witness | None:
    """
    The vision model to interrogate for this user, or None if there is none.

    None is a real answer, not an error: it means the `ask_vision` tool is not
    offered this turn and the user keeps today's "switch to a multimodal model"
    message. Offering a tool that cannot run is worse than not offering it.
    """
    provider, model = await _configured(user_id)

    if provider and model:
        if await sync_to_async(_has_key)(provider, user_id):
            # The user's pick leads; the platform chain still backs it up when
            # they share its provider, so a mistyped model id is survivable.
            rest = tuple(m for m in DEFAULT_CHAIN if m != model) if provider == DEFAULT_PROVIDER else ()
            return Witness(provider=provider, models=(model, *rest))
        logger.info("[Vision] No key for configured provider %s; falling back", provider)

    if await sync_to_async(_has_key)(DEFAULT_PROVIDER, user_id):
        return Witness(provider=DEFAULT_PROVIDER, models=DEFAULT_CHAIN)

    return None


async def witness_available(user_id: int | None) -> bool:
    """Cheap yes/no for the two callers that only need to know whether to offer."""
    if user_id is None:
        return False
    return await resolve_witness(user_id) is not None
