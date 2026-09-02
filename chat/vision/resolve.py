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

#: The platform default and its fallback, in order. Both measured against a
#: real image on 2026-09-01, each reading a rendered "4.8" correctly.
#: See docs/VISION_AGENT.md.
#:
#: Repointed 2026-09-01: both previous entries — nemotron-nano-12b-v2-vl and
#: llama-3.1-nemotron-nano-vl-8b-v1 — reached end of life on 2026-08-26 and NIM
#: answers 410 for both. A chain does not help when every link retires on the
#: same day, which is why `witness_available` now checks the chain against the
#: models the registry still lists as live rather than only checking that a key
#: exists.
#:
#: Ordered by *measured latency*, not by parameter count. The 11B answers in
#: ~1.2s; llama-3.2-90b-vision timed out at 90s on four consecutive attempts
#: and is deliberately not in the chain — a witness is asked up to six
#: questions in a turn, so a model that needs longer than the turn is not a
#: better witness, it is no witness. The omni-nano backs it up (~2.4s when it
#: has capacity, 503 when it does not, which is exactly what a fallback is for).
DEFAULT_PROVIDER = "nvidia"
DEFAULT_CHAIN: tuple[str, ...] = (
    "meta/llama-3.2-11b-vision-instruct",
    "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning",
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


def _retired(provider: str, models: tuple[str, ...]) -> frozenset[str]:
    """Which of `models` the AIModel registry explicitly marks retired.

    Absent means "unknown", not "dead" — the same rule `tools_config` uses for
    a missing row. A registry that has never been seeded (a fresh test
    database, say) must not silently withdraw everyone's eyes; only a row that
    is actually present and `is_active=False` is evidence. That makes the
    retirement list in `populate_models.py` load-bearing: retiring a vision
    model there withdraws the `ask_vision` tool instead of leaving it
    advertised and failing on every call, which is what happened when both
    chain entries reached end of life on the same day.
    """
    if not models:
        return frozenset()
    try:
        from llm.models import AIModel

        return frozenset(
            AIModel.objects.filter(
                provider__slug=provider, value__in=models, is_active=False,
            ).values_list("value", flat=True)
        )
    except Exception:  # noqa: BLE001
        # Same posture as the credential lookup below: the registry is not the
        # feature. If it cannot be read, believe nothing is retired rather than
        # stripping a working witness on the strength of a failed query.
        logger.warning("[Vision] Retirement lookup failed for %s", provider,
                       exc_info=True)
        return frozenset()


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
            candidates = (model, *rest)
            dead = await sync_to_async(_retired)(provider, candidates)
            if live := tuple(m for m in candidates if m not in dead):
                return Witness(provider=provider, models=live)
            logger.info(
                "[Vision] Every candidate for %s is retired; no witness", provider,
            )
            return None
        logger.info("[Vision] No key for configured provider %s; falling back", provider)

    if await sync_to_async(_has_key)(DEFAULT_PROVIDER, user_id):
        dead = await sync_to_async(_retired)(DEFAULT_PROVIDER, DEFAULT_CHAIN)
        if live := tuple(m for m in DEFAULT_CHAIN if m not in dead):
            return Witness(provider=DEFAULT_PROVIDER, models=live)
        logger.error(
            "[Vision] The whole default chain is retired — ask_vision will not "
            "be offered. Repoint DEFAULT_CHAIN in chat/vision/resolve.py.",
        )

    return None


async def witness_available(user_id: int | None) -> bool:
    """Cheap yes/no for the two callers that only need to know whether to offer."""
    if user_id is None:
        return False
    return await resolve_witness(user_id) is not None
