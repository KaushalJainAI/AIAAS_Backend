"""
What one request costs, and how it may be cut down when it costs too much.

Split out of `access.py` because three layers now need the same arithmetic and
the same notion of a *segment*: the final guard in `clamp_input`, the curator in
`chat/turn/curation.py`, and any caller sizing a transcript before it builds
one. Two copies of "how big is this history" is how a budget quietly stops
matching the thing it is budgeting.

The unit here is deliberately the segment, not the message. An assistant message
carrying `tool_calls` and the `tool` messages answering its call ids are one
indivisible thing on the wire: drop the assistant and the results become orphans
referring to a call id that is no longer present, which every OpenAI-compatible
provider rejects with a 400. Trimming used to walk messages one at a time and
had exactly that failure mode — a long agent run did not degrade, it died, after
the user had already paid for the tool calls.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Any, Iterable, Sequence

from workflow_backend.thresholds import MAX_LLM_INPUT_TOKENS

logger = logging.getLogger(__name__)

#: Room left for the model's own reply plus the per-message scaffolding no
#: estimate here can see (role names, delimiters, provider-side additions).
#: Deliberately generous: overshooting the window is a hard 400, undershooting
#: costs a little context.
_WINDOW_SAFETY_TOKENS = 2_048

#: `AIModel.context_window` is admin-editable, so it is cached with a TTL rather
#: than for the life of the process.
_WINDOW_TTL_SECONDS = 300
_window_cache: dict[str, tuple[float, int]] = {}


# ── Sizing ───────────────────────────────────────────────────────────────────

def estimate_tokens(text: str | None) -> int:
    """Approximate token count. ~4 chars per token for English prose."""
    return len(text) // 4 if text else 0


def content_tokens(content: Any) -> int:
    """Size a message body: a string on most entries, a list of parts on a
    multimodal one."""
    if isinstance(content, str):
        return estimate_tokens(content)
    if isinstance(content, list):
        # Image parts carry a url or a base64 blob whose cost is a provider
        # matter, not a character count; text parts are counted normally.
        return sum(
            estimate_tokens(part.get("text"))
            for part in content
            if isinstance(part, dict) and isinstance(part.get("text"), str)
        )
    return 0


def entry_tokens(entry: dict) -> int:
    """
    Everything in one wire entry that costs tokens, not only its text.

    A tool-calling assistant entry has `content: None` and carries the call in
    `tool_calls`, where the serialised `arguments` are frequently the largest
    thing in the turn. Counting only `content` scored those entries at zero, so a
    transcript made almost entirely of tool calls measured as nearly empty and
    the budget it was checked against meant nothing.
    """
    total = content_tokens(entry.get("content"))

    for call in entry.get("tool_calls") or []:
        function = call.get("function") or {}
        arguments = function.get("arguments")
        if not isinstance(arguments, str):
            arguments = json.dumps(arguments or {})
        total += estimate_tokens(function.get("name")) + estimate_tokens(arguments)
        total += 8  # id + envelope

    if entry.get("tool_call_id"):
        total += 8

    return total


def history_tokens(entries: Iterable[dict]) -> int:
    return sum(entry_tokens(entry) for entry in entries)


def truncate_middle(text: str, max_tokens: int) -> str:
    """
    Cut the middle out of an oversized string, keeping both ends.

    Head-only truncation is the obvious approach and the wrong one here: the tail
    of a user turn is usually the actual question ("...given all that, which
    should I pick?"). Losing it leaves the model a pile of context and no task.
    """
    max_chars = max_tokens * 4
    if len(text) <= max_chars:
        return text
    keep = max_chars // 2
    return (
        f"{text[:keep]}\n\n"
        f"[... {len(text) - 2 * keep} characters trimmed to fit the context window ...]\n\n"
        f"{text[-keep:]}"
    )


# ── Segments ─────────────────────────────────────────────────────────────────

@dataclass(slots=True)
class Segment:
    """One indivisible run of wire entries.

    Either a single standalone message (a user turn, a plain assistant answer, a
    system notice) or an assistant tool-call turn together with every `tool`
    result answering it. Nothing may drop, collapse or reorder part of one.
    """

    entries: list[dict]

    @property
    def tokens(self) -> int:
        return history_tokens(self.entries)

    @property
    def head(self) -> dict:
        return self.entries[0]

    @property
    def role(self) -> str:
        return str(self.head.get("role") or "")

    @property
    def is_tool_turn(self) -> bool:
        """An assistant turn that called tools — the only compactable shape."""
        return bool(self.head.get("tool_calls"))

    @property
    def results(self) -> list[dict]:
        return [entry for entry in self.entries[1:] if entry.get("role") == "tool"]


def split_segments(history: Sequence[dict]) -> list[Segment]:
    """Group wire history into segments, preserving order exactly.

    A `tool` entry with no assistant tool-call turn ahead of it is already
    broken; it is attached to whatever precedes it rather than promoted to a
    segment of its own, so that trimming cannot make the breakage worse by
    keeping the orphan and dropping its neighbours.
    """
    segments: list[Segment] = []
    for entry in history:
        if entry.get("role") == "tool" and segments:
            segments[-1].entries.append(entry)
            continue
        segments.append(Segment(entries=[entry]))
    return segments


def flatten(segments: Iterable[Segment]) -> list[dict]:
    return [entry for segment in segments for entry in segment.entries]


# ── Model-derived budget ─────────────────────────────────────────────────────

def cached_input_budget(model: str, *, reserve_output: int = 0) -> int:
    """`input_budget_for` without the database read — cache or nothing.

    `_build_request` is on every model call, including a streamed guest turn
    whose key is resolved lazily as the response is consumed. Awaiting a query
    there adds a suspension point inside the request path, which is enough to
    reorder when later work observes process state — it broke a guest test that
    patches the environment around the call. So the hot path reads what is
    already known and the lookup happens in `preflight`, which is async
    already, runs once per turn, and is where every other "can this call be
    made" question is answered.

    A cold cache means the flat ceiling, which is exactly the behaviour this
    replaced: never worse than before, better as soon as a turn has preflighted.
    """
    cached = _window_cache.get(model or "")
    window = cached[1] if cached else 0
    return _budget_from_window(window, reserve_output)


async def prime(model: str) -> None:
    """Fetch and cache `model`'s context window. Never raises."""
    await context_window(model)


def _budget_from_window(window: int, reserve_output: int) -> int:
    if window <= 0:
        return MAX_LLM_INPUT_TOKENS
    usable = window - max(reserve_output, 0) - _WINDOW_SAFETY_TOKENS
    return max(min(usable, MAX_LLM_INPUT_TOKENS), 2_000)


async def input_budget_for(model: str, *, reserve_output: int = 0) -> int:
    """
    How many input tokens this model can actually be sent.

    `MAX_LLM_INPUT_TOKENS` stays the ceiling — it is a cost control, not a
    capability claim — so a declared window can only ever *lower* the budget. The
    bug this fixes is the other direction: a flat 96k was applied to every model
    including the 8k and 32k ones, so the guard passed and the provider rejected
    the request.

    A model absent from the registry, or one whose `context_window` is 0
    ("unknown/variable"), keeps the flat budget: guessing small would silently
    throw away context the model could have read.
    """
    # Never returns something unusable: a tiny window still gets a floor, and the
    # per-message clamps are what keep that floor honest.
    return _budget_from_window(await context_window(model), reserve_output)


async def context_window(model: str) -> int:
    """`AIModel.context_window` for `model`, or 0 when unknown."""
    if not model:
        return 0

    cached = _window_cache.get(model)
    now = time.monotonic()
    if cached and now - cached[0] < _WINDOW_TTL_SECONDS:
        return cached[1]

    try:
        from llm.models import AIModel

        window = await AIModel.objects.filter(
            value=model
        ).values_list("context_window", flat=True).afirst()
    except Exception:  # noqa: BLE001
        # Sizing a request must not depend on the registry being reachable.
        logger.debug("[Budget] Context window lookup failed for %r", model, exc_info=True)
        return 0

    resolved = int(window or 0)
    _window_cache[model] = (now, resolved)
    return resolved
