"""
The witness loop: `ask(attachment, question)` -> an answer, always a string.

Two rules shape everything here.

**It never raises into the main turn.** A dead witness degrades to "I could not
examine that file" — a tool result the main agent can reason about and tell the
user about — not a 500 in the middle of someone's chat.

**It is bounded.** A main agent stuck on an ambiguous image will otherwise
interrogate it forever at the user's expense, and the user never asked for the
tokens.
"""
from __future__ import annotations

import asyncio
import logging
import re
from collections import OrderedDict

from asgiref.sync import sync_to_async

from llm import access as llm
from . import nim
from .prompts import DISAGREEMENT_NOTE, WITNESS_SYSTEM, wants_verbatim
from .resolve import PARSE_MODEL, resolve_witness

logger = logging.getLogger(__name__)

#: What the main agent is told when there is nothing to ask. Phrased as an
#: observation it can pass on, because the alternative — silence, or an
#: exception — is what makes a model invent an answer instead.
VISION_UNAVAILABLE = (
    "I could not examine that file: no vision model is available right now. "
    "Tell the user you cannot see it rather than guessing at its contents."
)

#: Questions per attachment per turn. Six is enough for a real cross-examination
#: (describe, then four or five specifics) and short of an infinite loop.
MAX_QUESTIONS_PER_TURN = 6

#: Prior exchanges replayed to the witness. The image dominates the context
#: window; the transcript is there so follow-ups cohere, not to be complete.
TRANSCRIPT_LIMIT = 4

WITNESS_TIMEOUT_SECONDS = 60.0
WITNESS_ATTEMPTS = 2
WITNESS_BACKOFF_SECONDS = 1.0
MAX_ANSWER_CHARS = 4_000

#: Substrings that mean "this model is not callable" rather than "this call
#: failed". NIM answers 404 for models its own catalog advertises, so the chain
#: has to walk past them instead of giving up on the first one.
#:
#: 410 / "end of life" joined the list on 2026-09-01. A retired model is the
#: one case where retrying is guaranteed to be wasted — it is permanent, and
#: it is announced by a *different* status than the 404 this list was built
#: for, so both chain entries burned their retries and then failed instead of
#: falling through. Treat it exactly like unentitlement: walk to the next.
_UNENTITLED_MARKERS = (
    "404", "not found for account", "unknown model", "does not exist",
    "410", "end of life", "no longer available",
)

#: Errors that say something about *this model* rather than about the account,
#: so the chain should try the next candidate instead of giving up.
#:
#: "timeout" joined "unentitled" on 2026-09-01. A timeout was treated as
#: account-wide and broke the loop, so one slow model blinded the agent
#: entirely — measured: llama-3.2-90b-vision timed out at 60s while the 11B
#: behind it in the chain answered the same image in 1.2s, and the fallback
#: never ran. Capacity is per model; a key is not.
_CHAIN_CONTINUES = frozenset({"unentitled", "timeout"})

#: (turn_id, attachment_id) -> questions asked. Bounded because a chat process is
#: long-lived: without the cap this is a slow leak of every turn ever run.
_turn_budget: OrderedDict[tuple[str, str], int] = OrderedDict()
_BUDGET_ENTRIES = 512


def _spend_budget(turn_id: str, attachment_id) -> bool:
    """Charge one question to this turn. False when the cap is already spent."""
    if not turn_id:
        # No turn identity (a direct tool invocation, a test) means no budget to
        # enforce. The per-call timeout and the iteration cap still bound it.
        return True

    key = (turn_id, str(attachment_id))
    used = _turn_budget.get(key, 0)
    if used >= MAX_QUESTIONS_PER_TURN:
        return False

    _turn_budget[key] = used + 1
    _turn_budget.move_to_end(key)
    while len(_turn_budget) > _BUDGET_ENTRIES:
        _turn_budget.popitem(last=False)
    return True


@sync_to_async
def _load_transcript(session_id: str, attachment_id) -> list[dict[str, str]]:
    """The last few exchanges about this attachment, oldest first, wire-shaped."""
    from ..models import VisionExchange

    rows = list(
        VisionExchange.objects
        .filter(session_id=session_id, attachment_id=attachment_id)
        .order_by("-created_at")
        .values("question", "answer")[:TRANSCRIPT_LIMIT]
    )
    rows.reverse()
    wire: list[dict[str, str]] = []
    for row in rows:
        wire.append({"role": "user", "content": row["question"]})
        wire.append({"role": "assistant", "content": row["answer"]})
    return wire


@sync_to_async
def _record(session_id: str, attachment, question: str, answer: str, *,
            model: str, disagreement: bool) -> None:
    from ..models import VisionExchange

    VisionExchange.objects.create(
        session_id=session_id,
        attachment=attachment,
        question=question,
        answer=answer,
        model=model,
        disagreement=disagreement,
    )


#: Standalone numbers only. The lookarounds are load-bearing: without them "Q3"
#: contributes a 3, and a witness answer that names the quarter it was asked
#: about "disagrees" with every parse that does not repeat the label — which
#: fires the uncertainty warning on correct readings, the one thing that would
#: make the whole signal worth ignoring.
_NUMBER = re.compile(r"(?<![A-Za-z0-9._-])[-+]?\d[\d,]*(?:\.\d+)?(?![A-Za-z0-9_])")


def _numbers(text: str) -> set[str]:
    """Numeric tokens in `text`, comma-stripped and value-normalised."""
    found = set()
    for raw in _NUMBER.findall(text or ""):
        cleaned = raw.replace(",", "").lstrip("+")
        try:
            found.add(f"{float(cleaned):g}")
        except ValueError:
            continue
    return found


def readings_diverge(answer: str, parsed: str) -> bool:
    """
    Whether the witness and the parser read the same glyphs differently.

    This is the entire uncertainty mechanism, and it is here because the failure
    it catches cannot be caught any other way: the misread is deterministic at
    temperature 0, so retries return the same wrong number, and it is confident,
    so the model's own wording gives nothing away. Two cheap models disagreeing
    is the only signal on offer.

    Conservative on purpose — a false alarm makes the main agent hedge a correct
    number, which is a cost, just a much smaller one than a wrong number stated
    plainly.
    """
    said = _numbers(answer)
    seen = _numbers(parsed)
    if not said or not seen:
        return False
    return bool(said - seen)


async def _call_witness(
    *, provider: str, model: str, question: str, transcript: list[dict[str, str]],
    attachment, user_id: int,
) -> tuple[str | None, str | None]:
    """`(answer, error)` for one model. Retries transient failures, not 404s."""
    for attempt in range(WITNESS_ATTEMPTS):
        try:
            async with asyncio.timeout(WITNESS_TIMEOUT_SECONDS):
                completion = await llm.complete(
                    provider=provider,
                    model=model,
                    prompt=question,
                    system_message=WITNESS_SYSTEM,
                    user_id=user_id,
                    # The witness reads; it does not invent. Temperature buys
                    # nothing here and costs consistency between the two models
                    # whose agreement is the only check there is.
                    temperature=0.0,
                    max_tokens=1024,
                    history=list(transcript),
                    attachments=[attachment],
                )
        except asyncio.TimeoutError:
            logger.warning("[Vision] %s timed out after %ss", model,
                           WITNESS_TIMEOUT_SECONDS)
            return None, "timeout"
        except llm.LLMModelUnavailable:
            return None, "unentitled"
        except llm.LLMUserActionable as exc:
            # Credentials or credit — the user's to fix, and no other model on
            # the same key will do better.
            return None, str(exc)
        except Exception as exc:  # noqa: BLE001
            text = str(exc).lower()
            if any(marker in text for marker in _UNENTITLED_MARKERS):
                logger.info("[Vision] %s unavailable for this account", model)
                return None, "unentitled"
            logger.warning("[Vision] %s failed (attempt %d): %s",
                           model, attempt + 1, exc)
            if attempt + 1 < WITNESS_ATTEMPTS:
                await asyncio.sleep(WITNESS_BACKOFF_SECONDS * (attempt + 1))
                continue
            return None, str(exc)

        if answer := (completion.content or "").strip():
            return answer[:MAX_ANSWER_CHARS], None

        # An empty body is not an answer. Retry once; a second empty one is a
        # real result, and saying so beats returning "" to the main agent.
        if attempt + 1 < WITNESS_ATTEMPTS:
            await asyncio.sleep(WITNESS_BACKOFF_SECONDS)
            continue
        return None, "empty response"

    return None, "exhausted"


async def _cross_check(attachment, answer: str, *, user_id: int) -> str | None:
    """The parser's reading of the same image, when it disagrees. Else None."""
    from credentials.resolution import resolve_api_key

    try:
        api_key = await resolve_api_key("nvidia", user_id, require_verified=False)
    except Exception:  # noqa: BLE001
        return None
    if not api_key:
        return None

    parsed = await nim.parse_image(attachment, api_key=api_key, model=PARSE_MODEL)
    if not parsed:
        return None
    return parsed if readings_diverge(answer, parsed) else None


async def ask(
    attachment, question: str, *, session_id: str, user_id: int, turn_id: str = "",
) -> str:
    """
    Put one question to the witness about `attachment` and return its answer.

    Always returns a string. Every failure path produces a sentence the main
    agent can act on, because the one thing it must not do is leave that agent
    with nothing and let it fill the gap by inference.
    """
    question = (question or "").strip()
    if not question:
        return "No question was asked about the file."

    if not _spend_budget(turn_id, getattr(attachment, "id", "")):
        return (
            f"You have already asked {MAX_QUESTIONS_PER_TURN} questions about "
            f"this file in one turn, which is the limit. Answer the user with "
            f"what you have, and say what remains unclear."
        )

    witness = await resolve_witness(user_id)
    if witness is None:
        return VISION_UNAVAILABLE

    transcript = await _load_transcript(session_id, getattr(attachment, "id", None))

    answer: str | None = None
    used_model = ""
    last_error = ""
    for model in witness.models:
        answer, error = await _call_witness(
            provider=witness.provider, model=model, question=question,
            transcript=transcript, attachment=attachment, user_id=user_id,
        )
        if answer is not None:
            used_model = model
            break
        last_error = error or ""
        if error not in _CHAIN_CONTINUES:
            # A rejected key or exhausted credit repeats on every model on the
            # same account, so walking the chain just spends the user's turn
            # re-learning it. Only per-model failures are worth walking past.
            break

    if answer is None:
        logger.info("[Vision] No witness answered: %s", last_error or "unknown")
        return VISION_UNAVAILABLE

    disagreement = False
    if wants_verbatim(question):
        try:
            if parsed := await _cross_check(attachment, answer, user_id=user_id):
                answer += DISAGREEMENT_NOTE.format(parsed=parsed[:400])
                disagreement = True
        except Exception:  # noqa: BLE001
            # The cross-check is a second opinion. Losing it means the answer
            # goes out unqualified, which is where we were before it existed.
            logger.warning("[Vision] Cross-check failed", exc_info=True)

    try:
        await _record(session_id, attachment, question, answer,
                      model=used_model, disagreement=disagreement)
    except Exception:  # noqa: BLE001
        # The transcript is memory and audit, not the answer. Failing to write
        # it must not throw away a look the user has already paid for.
        logger.warning("[Vision] Could not record exchange", exc_info=True)

    return answer
