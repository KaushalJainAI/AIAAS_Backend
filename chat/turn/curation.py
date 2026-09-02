"""
Keeping a long run inside its context window, deliberately and on the record.

An agent run rebuilds its whole transcript on every iteration: `agent_node`
sends `history + to_wire(state["messages"])`, and that list only ever grows — up
to 40 iterations, each able to carry tool results of up to
`TOOL_OUTPUT_CHAR_LIMIT`. Something has to give, and until this module existed
the only thing that did was `llm.clamp_input`, which drops the oldest segments
blindly at the last possible moment and tells the model only that *something*
went.

Three mechanisms, each a toggle on the agent (`runtime_settings`), each doing
strictly less damage than the one after it:

- **compaction** — mechanical, free, no model call. An old tool-calling turn
  keeps its reasoning and the *names and arguments* of what it called; the
  results themselves shrink to a short record. What a step did stays legible;
  what it returned becomes a pointer.
- **recursiveContext** — when compaction alone cannot reach the low mark, the
  oldest steps are folded into a single running note, and that note is folded
  back in with the next block as the window refills. The only mechanism that
  costs a model call.
- **indexing** — everything the other two remove is archived first, and the
  record left behind names the id. This is what makes the other two safe rather
  than merely cheap: with indexing off, what is dropped is actually gone, and
  the notices left behind say so instead of pointing at an id nobody wrote.

Four properties are non-negotiable:

*It edits state, not the wire.* Curation returns replacement messages carrying
the ids they replace, so `add_messages` folds them into the checkpoint and the
work is done once. Curating the outgoing copy instead would recompute — and
re-archive — the same text on every remaining turn of the run.

*Segments, never messages.* An assistant turn carrying `tool_calls` and the
`tool` messages answering it move together or not at all. Splitting them leaves
a `tool_call_id` referring to a call that is no longer present, which providers
answer with a 400.

*A watermark, not a trickle.* Curation fires only when the transcript crosses
`CONTEXT_HIGH_WATER_RATIO` of the budget, then cuts to `CONTEXT_LOW_WATER_RATIO`
in one pass. Shaving a little every turn would change the request prefix on
every single call and forfeit provider prefix caching — the same reason the
clock does not live in the system message.

*There is exactly one summary note.* A second note is not more memory, it is the
same run written down twice; the fold always absorbs the previous note.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Sequence

from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    RemoveMessage,
    SystemMessage,
    ToolMessage,
)

from llm import budget
from workflow_backend.thresholds import (
    CONTEXT_HIGH_WATER_RATIO,
    CONTEXT_KEEP_RECENT_SEGMENTS,
    CONTEXT_LOW_WATER_RATIO,
    CONTEXT_SUMMARY_INPUT_CHARS,
    CONTEXT_SUMMARY_TARGET_WORDS,
    CONTEXT_TOOL_RECORD_CHARS,
)

logger = logging.getLogger(__name__)

#: Marks the stand-in left where a tool result used to be. Recognisable in a
#: transcript dump, and recognisable to the next pass, so nothing is compacted
#: or archived twice.
RECORD_PREFIX = "[COMPACTED RESULT]"
#: Marks the single running note. Identified by prefix rather than by a message
#: id we would have to carry in state: the note is regenerated on every fold, so
#: a stored id would be one more thing that can go stale.
SUMMARY_PREFIX = "[EARLIER WORK IN THIS RUN — SUMMARY]"


@dataclass(frozen=True, slots=True)
class CurationPolicy:
    """What a caller has asked the curator to be allowed to do.

    Chat leaves this at the default and is therefore unchanged: `enabled=False`
    means `curate` returns nothing to apply, and the only bound on the window
    stays `clamp_input`, exactly as before. The agent runtime builds a real
    policy from `SubAgent.runtime_settings`, so callers differ in configuration
    rather than in code path.
    """

    enabled: bool = False
    compaction: bool = True
    recursive: bool = True
    indexing: bool = True

    #: Model used for the fold. Deliberately not the agent's own by default: a
    #: 40-turn run on an expensive model would otherwise pay full rate to
    #: compress itself. Empty means "use the run's own model".
    summary_provider: str = ""
    summary_model: str = ""

    @classmethod
    def from_settings(cls, settings: dict | None) -> CurationPolicy:
        """Build a policy from one agent's `runtime_settings`.

        The three keys are the context-lifecycle toggles the agent builder has
        always stored: `compaction`, `recursiveContext`, `indexing`. They
        defaulted to True in the serializer while nothing read them, so the
        defaults here match — turning the feature on must not silently change
        what an existing agent does beyond finally honouring its own settings.
        """
        from django.conf import settings as django_settings

        settings = settings or {}
        compaction = bool(settings.get("compaction", True))
        recursive = bool(settings.get("recursiveContext", True))
        indexing = bool(settings.get("indexing", True))

        # The agent's own choice wins over the platform default, and the
        # platform default over the run's model. Precedence in that order
        # because each step is a narrower statement of intent: the user picked
        # this model for *this agent*, the operator picked one for the install,
        # and falling back to the run's own model is nobody's decision — just
        # the last thing that is certain to work.
        #
        # A model without a provider is not usable, so the two move together: an
        # agent that names a model but no provider is asking for that model on
        # the platform's provider, which is what `summary_provider` falls back
        # to.
        chosen_model = (settings.get("summaryModel") or "").strip()
        chosen_provider = (settings.get("summaryProvider") or "").strip()
        return cls(
            summary_provider=(
                chosen_provider
                or getattr(django_settings, "CONTEXT_SUMMARY_PROVIDER", "")
            ),
            summary_model=(
                chosen_model
                or getattr(django_settings, "CONTEXT_SUMMARY_MODEL", "")
            ),
            # All three off is a request to be left alone, and honouring it is
            # the point of a toggle: `clamp_input` still keeps the run from
            # 400ing, so "off" degrades to the old behaviour rather than to an
            # unbounded window.
            enabled=compaction or recursive,
            compaction=compaction,
            recursive=recursive,
            indexing=indexing,
        )


@dataclass(slots=True)
class CurationResult:
    """What one pass did: the state updates to apply, and the record of why."""

    updates: list[BaseMessage] = field(default_factory=list)
    results_compacted: int = 0
    steps_folded: int = 0
    tokens_before: int = 0
    tokens_after: int = 0
    archived_ids: tuple[str, ...] = ()
    summary_tokens: int = 0

    @property
    def curated(self) -> bool:
        return bool(self.results_compacted or self.steps_folded)

    @property
    def tokens_saved(self) -> int:
        return max(self.tokens_before - self.tokens_after, 0)


# ── Sizing and grouping, in message vocabulary ───────────────────────────────

def _wire(messages: Sequence[BaseMessage]) -> list[dict]:
    from .agent import to_wire

    return to_wire(messages)


def message_tokens(messages: Sequence[BaseMessage]) -> int:
    """What these messages will cost once rendered for the provider.

    Measured through `to_wire` rather than off `message.content`, so a
    tool-calling turn is charged for its serialised arguments — which are
    frequently the largest thing in it and which `content` does not contain.
    """
    return budget.history_tokens(_wire(messages))


@dataclass(slots=True)
class _Segment:
    """An assistant tool-call turn plus the results answering it, or one
    standalone message. The unit everything here moves in."""

    messages: list[BaseMessage]

    @property
    def head(self) -> BaseMessage:
        return self.messages[0]

    @property
    def tokens(self) -> int:
        return message_tokens(self.messages)

    @property
    def is_tool_turn(self) -> bool:
        return isinstance(self.head, AIMessage) and bool(self.head.tool_calls)

    @property
    def is_note(self) -> bool:
        return _is_note(self.head)


def _is_note(message: BaseMessage) -> bool:
    return (
        isinstance(message, SystemMessage)
        and isinstance(message.content, str)
        and message.content.startswith(SUMMARY_PREFIX)
    )


def segments(messages: Sequence[BaseMessage]) -> list[_Segment]:
    """Group messages into segments, order preserved.

    A `ToolMessage` with no tool-call turn ahead of it is already broken; it is
    attached to whatever precedes it rather than made a segment of its own, so
    curation cannot make the breakage worse by keeping the orphan and dropping
    its neighbours.
    """
    grouped: list[_Segment] = []
    for message in messages:
        if isinstance(message, ToolMessage) and grouped:
            grouped[-1].messages.append(message)
            continue
        grouped.append(_Segment(messages=[message]))
    return grouped


# ── Records ──────────────────────────────────────────────────────────────────

def _describe_call(turn: BaseMessage, call_id: str) -> str:
    """Name the call a result answers, so the record says what was asked."""
    if isinstance(turn, AIMessage):
        for call in turn.tool_calls or []:
            if call.get("id") != call_id:
                continue
            arguments = json.dumps(call.get("args") or {})
            return f"{call.get('name') or 'tool'}({arguments[:200]})"
    return "tool"


def _record_for(result: str, call: str, archive_id: str | None) -> str:
    """The text that replaces one tool result once it has been curated."""
    head = result[:CONTEXT_TOOL_RECORD_CHARS].strip()
    dropped = len(result) - len(head)

    where = (
        f"archived as tool_output id '{archive_id}' — call recall_context or "
        f"read_tool_output to read it"
        if archive_id else
        "not archived, so the removed text is gone; re-run the call if you "
        "need it"
    )
    return (
        f"{RECORD_PREFIX} {call}\n{head}\n"
        f"[... {dropped:,} characters removed to stay inside the context window. "
        f"{where}.]"
    )


def _already_compacted(message: BaseMessage) -> bool:
    return (
        isinstance(message.content, str)
        and message.content.startswith(RECORD_PREFIX)
    )


# ── The pass ─────────────────────────────────────────────────────────────────

async def curate(
    messages: Sequence[BaseMessage],
    *,
    policy: CurationPolicy,
    model: str,
    reserve_output: int = 0,
    baseline_tokens: int = 0,
    context: dict[str, Any] | None = None,
    summarise=None,
) -> CurationResult:
    """
    Decide what to remove from this run's transcript, and return it as updates.

    `baseline_tokens` is everything else already committed to the request — the
    system message and any conversation history outside the run — because the
    watermark has to be measured against the real total, not against the part
    that happens to live in graph state.

    `summarise` is an injected `async (text) -> (summary, tokens)`. Injected so
    this module never imports the provider layer, and so a test can fold without
    a model.
    """
    context = context or {}
    result = CurationResult()
    if not policy.enabled or not messages:
        return result

    limit = await budget.input_budget_for(model, reserve_output=reserve_output)
    result.tokens_before = baseline_tokens + message_tokens(messages)
    if result.tokens_before <= limit * CONTEXT_HIGH_WATER_RATIO:
        return result

    target = int(limit * CONTEXT_LOW_WATER_RATIO)
    grouped = segments(messages)

    # The tail is never curated: the model must be able to see what it just did,
    # or it does it again. Counted in segments rather than tokens so that one
    # oversized recent result cannot push three whole turns out of view.
    keep = min(CONTEXT_KEEP_RECENT_SEGMENTS, len(grouped))
    head = grouped[:-keep] if keep else list(grouped)
    tail = grouped[-keep:] if keep else []
    if not head:
        return result

    archived: list[str] = []
    updates: list[BaseMessage] = []

    def projected() -> int:
        return baseline_tokens + message_tokens(
            [m for segment in head + tail for m in segment.messages]
        )

    if policy.compaction:
        replacements, ids = await _compact(head, policy, context, target, projected)
        result.results_compacted = len(replacements)
        archived.extend(ids)
        updates.extend(replacements)

    if policy.recursive and summarise is not None and projected() > target:
        folded, note, ids, tokens = await _fold(
            head, tail, policy, context, target, baseline_tokens, summarise
        )
        if note is not None:
            # RemoveMessage by id is what actually deletes from the checkpoint;
            # the note is appended, which puts it last in the transcript. That
            # is where a trailing system message belongs anyway — the same shape
            # `build_context_update` uses, landing after the prior conversation
            # and before the model's next move.
            updates.extend(RemoveMessage(id=m.id) for m in folded if m.id)
            updates.append(note)
            result.steps_folded = len(folded)
            result.summary_tokens = tokens
        archived.extend(ids)

    result.updates = updates
    result.archived_ids = tuple(archived)
    result.tokens_after = projected()

    if result.curated:
        logger.info(
            "[Curation] %d result(s) compacted, %d message(s) folded: "
            "%d -> %d tokens (budget %d)",
            result.results_compacted, result.steps_folded,
            result.tokens_before, result.tokens_after, limit,
        )
    return result


async def _compact(
    head: list[_Segment],
    policy: CurationPolicy,
    context: dict[str, Any],
    target: int,
    projected,
) -> tuple[list[ToolMessage], list[str]]:
    """Shrink old tool results to records, oldest first, until the target is met.

    Oldest first because recency is the best available proxy for what is still
    needed, and stopping at the target leaves the newest of the old turns whole:
    a run that has only just crossed the mark loses only its earliest steps.

    Each replacement carries the id of the message it replaces, which is what
    makes `add_messages` substitute rather than append — the substitution is the
    point, since a checkpoint still holding the original text would be archived
    all over again on the next pass. `segment.messages` is updated too, so
    `projected()` measures the transcript as curated so far rather than as it
    arrived.
    """
    from chat.tools import tool_output

    replacements: list[ToolMessage] = []
    archived: list[str] = []

    for segment in head:
        if projected() <= target:
            break
        if not segment.is_tool_turn:
            continue

        for index, message in enumerate(segment.messages):
            if not isinstance(message, ToolMessage) or _already_compacted(message):
                continue
            if not message.id:
                # Without an id there is nothing for `add_messages` to replace,
                # and returning the shrunk copy anyway would append a second
                # result for the same call. `add_messages` stamps ids on
                # everything already in state, so this is a guard, not a case.
                continue
            content = message.content
            if not isinstance(content, str) or len(content) <= CONTEXT_TOOL_RECORD_CHARS:
                continue

            call = _describe_call(segment.head, message.tool_call_id)
            archive_id = None
            if policy.indexing:
                archive_id = await tool_output.archive(call, content, context)
                if archive_id:
                    archived.append(archive_id)

            replacement = ToolMessage(
                content=_record_for(content, call, archive_id),
                tool_call_id=message.tool_call_id,
                id=message.id,
                name=message.name,
            )
            segment.messages[index] = replacement
            replacements.append(replacement)

    return replacements, archived


async def _fold(
    head: list[_Segment],
    tail: list[_Segment],
    policy: CurationPolicy,
    context: dict[str, Any],
    target: int,
    baseline_tokens: int,
    summarise,
) -> tuple[list[BaseMessage], SystemMessage | None, list[str], int]:
    """
    Replace the oldest segments — and any previous note, wherever it sits — with
    one running note.

    The previous note is always absorbed: leaving it in place and appending a
    second one would grow the transcript by a note per curation, which is the
    problem this is here to solve rather than a smaller version of it.
    """
    from chat.tools import tool_output

    # What the note itself will cost. Folding more segments does not shrink
    # `fold + remainder`, so a condition measured over that total would never
    # change and the first pass would fold the entire run.
    note_tokens = CONTEXT_SUMMARY_TARGET_WORDS * 2

    fold: list[_Segment] = []
    remainder = list(head)
    while remainder:
        kept = [m for segment in remainder + tail for m in segment.messages]
        if baseline_tokens + note_tokens + message_tokens(kept) <= target:
            break
        fold.append(remainder.pop(0))

    # Any earlier note is folded in even when it is not in `fold` — it may be
    # sitting in the protected tail, which is exactly where the last pass left
    # it.
    if not fold:
        # Nothing new to fold. Rewriting the note on its own would spend a model
        # call to restate what it already says, on every pass, for the rest of
        # the run.
        return [], None, [], 0

    stragglers = [
        segment for segment in remainder + tail if segment.is_note
    ]

    folded_messages = [m for segment in fold for m in segment.messages]
    folded_messages += [m for segment in stragglers for m in segment.messages]

    transcript = _render_for_summary(folded_messages)
    archived: list[str] = []
    if policy.indexing:
        archive_id = await tool_output.archive("folded-steps", transcript, context)
        if archive_id:
            archived.append(archive_id)

    try:
        summary, tokens = await summarise(transcript[:CONTEXT_SUMMARY_INPUT_CHARS])
    except Exception:  # noqa: BLE001
        # A failed fold must not fail the run. Nothing is applied, the messages
        # stay where they are, and `clamp_input` drops them at the wire if the
        # request is still too large — worse, but not an outage.
        logger.exception("[Curation] Fold failed; transcript left intact")
        return [], None, archived, 0

    if not summary.strip():
        return [], None, archived, tokens

    note = SUMMARY_PREFIX + "\n" + summary.strip()
    note += (
        f"\n[Full text of these steps is archived as tool_output id "
        f"'{archived[-1]}'. Call recall_context or read_tool_output for any "
        f"detail this summary does not carry.]"
        if archived else
        "\n[The steps behind this summary were not archived; this note is all "
        "that remains of them.]"
    )
    return folded_messages, SystemMessage(content=note), archived, tokens


def _render_for_summary(messages: Sequence[BaseMessage]) -> str:
    """Flatten messages into something a summariser can read.

    A previous note renders like anything else and is labelled by its own
    prefix, which is what makes the fold recursive rather than merely repeated.
    """
    lines: list[str] = []
    for message in messages:
        content = message.content
        text = content if isinstance(content, str) else json.dumps(content)
        if isinstance(message, AIMessage) and message.tool_calls:
            calls = ", ".join(
                f"{call.get('name')}({json.dumps(call.get('args') or {})[:200]})"
                for call in message.tool_calls
            )
            lines.append(f"[assistant called: {calls}]\n{text or ''}")
        elif isinstance(message, ToolMessage):
            lines.append(f"[result of {message.name or 'tool'}]\n{text or ''}")
        else:
            lines.append(f"[{message.type}]\n{text or ''}")
    return "\n\n".join(lines)


SUMMARY_INSTRUCTION = (
    "You are compressing the earlier steps of an agent run so the agent can keep "
    "working without them in view. Write a factual record of at most "
    f"{CONTEXT_SUMMARY_TARGET_WORDS} words, in plain prose, covering: what was "
    "attempted, what was established as fact — with the specific values, names, "
    "ids and figures, which are what a later step will need — what failed and "
    "why, and what is still open. Do not address the user, do not add "
    "conclusions of your own, and never state a detail that is not in the text. "
    "If an earlier summary appears in the input, fold it in rather than "
    "repeating it verbatim."
)
