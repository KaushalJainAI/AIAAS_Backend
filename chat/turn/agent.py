"""
The chat agent: a LangGraph tool loop over real chat messages.

The important property of this module — the one the previous implementation
lacked — is that the model sees a *proper transcript*. An assistant turn that
requested tools is sent back as an assistant message carrying `tool_calls`, and
each result as a `tool` message with the matching `tool_call_id`.

That is not a stylistic preference. Flattening tool results into prose ("---
PREVIOUS ACTIONS AND TOOL RESULTS ---") takes the model off the distribution it
was trained on and it starts *imitating* tool calls in text instead of emitting
them, which is why the old code needed a 20-pattern scraper to read them back.
Thread the messages correctly and native tool calls just work; the scraper in
`extraction.py` shrinks to a fallback for weak local models.

Read-only turn settings live in `TurnContext` on the runnable config, not in
graph state, so the checkpointer only ever persists what actually changes.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Annotated, Any, Awaitable, Callable, Sequence, TypedDict
from uuid import uuid4

from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.memory import MemorySaver
from langgraph.errors import GraphInterrupt
from langgraph.graph import END, StateGraph
from langgraph.graph.message import add_messages
from langgraph.types import interrupt

from workflow_backend.thresholds import MAX_TOOL_ITERATIONS

from llm import access as llm
from . import prompts
from .events import Event, EventSink, null_sink
from llm.access import (
    Completion,
    LLMUnavailable,
    LLMUserActionable,
    StreamAccumulator,
    humanize_provider_body,
    ToolCall,
)

logger = logging.getLogger(__name__)

LLM_CALL_TIMEOUT = 180
TOOL_CALL_TIMEOUT = 120

#: Output room by intent. Long-form work needs more than a chat reply.
_MAX_TOKENS_BY_INTENT: dict[str, int] = {
    "coding": 16_384,
    "research": 16_384,
    "file_manipulation": 16_384,
}
_DEFAULT_MAX_TOKENS = 8_192

_NO_PROVIDER_MESSAGE = (
    "I can't reach a language model right now. Add and verify a provider "
    "credential (for example OpenRouter) in Settings, then try again."
)


# ── Turn configuration ───────────────────────────────────────────────────────

#: Returns the OpenAI-shaped tool descriptors the model may see this turn.
ToolSource = Callable[[], Awaitable[list[dict[str, Any]]]]
#: Runs one tool call: (name, arguments, context) -> result text.
ToolDispatch = Callable[[str, dict[str, Any], dict[str, Any]], Awaitable[str]]
#: Decides whether one call needs a human, beyond the `sensitive_tools` names:
#: (name, arguments, tool context) -> True to pause. It exists because MCP tool
#: names are minted at runtime from a third-party catalogue, so no static list
#: can hold them, and `credential_injector` hands them the user's real keys.
#: See `chat.permissions` for the rules and why reads are exempt in chat.
ApprovalPolicy = Callable[[str, dict[str, Any], dict[str, Any]], Awaitable[bool]]
#: Observes one finished tool call. Called after dispatch returns *or* raises,
#: so a caller recording a trace sees failures as well as successes. Takes the
#: keyword payload described on `TurnContext.on_tool_result`. Must not raise:
#: an observer that throws would fail a tool call that actually succeeded.
ToolObserver = Callable[..., Awaitable[None]]
#: Observes one finished model call — the *turn*, which is the unit an agent
#: actually reasons in. Called once per pass of `model_node`, after the
#: completion is in hand, with the keyword payload described on
#: `TurnContext.on_model_turn`. Must not raise, for the same reason
#: `ToolObserver` must not: watching a run may not break it.
TurnObserver = Callable[..., Awaitable[None]]

@dataclass(frozen=True, slots=True)
class TurnContext:
    """Read-only settings for one turn. Lives on the config, not in state."""

    provider: str
    model: str
    system_message: str
    user_id: int
    session_id: str
    intent: str
    user_text: str
    history: tuple[dict[str, Any], ...] = ()
    attachments: tuple[Any, ...] = ()
    memory_enabled: bool = True
    max_iterations: int = MAX_TOOL_ITERATIONS
    sink: EventSink = null_sink

    #: Sampling temperature for this turn's model calls. Defaults to `llm.stream`'s
    #: own default so chat behaves exactly as before; the agent runtime overrides it
    #: from `SubAgent.runtime_settings['temperature']`. Before this field existed the
    #: builder's temperature slider was stored, round-tripped to the UI, and then
    #: dropped on the floor — every agent turn ran at 0.7 whatever the user chose.
    temperature: float = 0.7

    #: Identity for this turn, generated rather than passed: it exists only so a
    #: tool can meter itself per turn (`ask_vision` caps how many times it will
    #: interrogate one image before the user is paying for a loop). `session_id`
    #: cannot do that job — it spans every turn in the conversation — and
    #: `thread_id` is not visible from inside a tool.
    turn_id: str = field(default_factory=lambda: uuid4().hex)

    # Optional overrides that let a non-chat caller — the agent runtime —
    # own which tools exist, how they run, and which need approval. The
    # agent runtime must gate on a grant list the chat registry knows
    # nothing about, and a denied grant has to fail at call time, not just
    # go unadvertised. Left None, the chat defaults apply unchanged.
    tool_source: ToolSource | None = None
    tool_dispatch: ToolDispatch | None = None
    sensitive_tools: frozenset[str] | None = None

    #: Which knowledge bases the KB tools may reach this turn, or None for "any
    #: the user owns". The agent runtime sets it from the builder's KB selection
    #: — which until now was read only to print names into the system prompt,
    #: while `knowledge_base_search` happily resolved any KB the user owned. A
    #: selector that narrows nothing is the permissions screen lying.
    #:
    #: None, not an empty tuple, is unrestricted: chat has no selection to make,
    #: and an agent whose selection is empty never had one enforced, so
    #: enforcing it now as "nothing" would silently empty its corpus.
    kb_scope: tuple[int, ...] | None = None

    #: Archives from *other* runs this turn may read, on top of its own. Set to
    #: the parent's session id for a delegated worker, and empty everywhere
    #: else. Read-only and one hop: a worker never writes into its parent's
    #: archive, and never sees a grandparent's.
    #:
    #: It exists because curation and delegation otherwise work against each
    #: other — a parent that curated a detail away cannot restate it in the
    #: task, and the worker, being a fresh thread, could not reach it either.
    archive_scopes: tuple[str, ...] = ()

    #: The `inference.vfs.FileScope` the file tools address this turn, or None.
    #: Chat always leaves it None — a chat turn has no `fileAccess` setting to
    #: build one from, which is why the file tools' `requires="files"` is never
    #: met there. The agent runtime sets it from `sandbox['fileAccess']`.
    file_scope: Any = None

    #: How many agents deep this turn already is. 0 is a run the user started;
    #: a worker spawned by `invoke_subagent` gets its parent's depth plus one.
    #: Delegation is refused past `MAX_DELEGATION_DEPTH` — without a counter,
    #: an agent holding the `subAgents` grant can invoke an agent holding the
    #: `subAgents` grant, and the cost of that is multiplicative.
    depth: int = 0

    #: `agents.budget.Deadline`, or None in chat. The instant this run must be
    #: finished by. Read in two places and nowhere else: `agent_node` stops
    #: asking for tools once it is `wrapping_up`, and `tools_node` passes it to
    #: delegating tools so a worker cannot outlive the run that asked for it.
    #: Typed `Any` for the same reason `file_scope` is — chat has no concept of
    #: an agent budget, and importing one here to name it would invert the
    #: dependency between this module and `agents/`.
    deadline: Any = None

    #: Consulted for calls `sensitive_tools` does not already name. Left None,
    #: chat's own policy applies. The agent runtime supplies a stricter one for
    #: unattended runs and an empty one for `autonomy='full'`, where the user
    #: has explicitly asked not to be interrupted.
    approval_policy: ApprovalPolicy | None = None

    #: What each autonomy level means, for the levels a user may switch to
    #: while the run is going: level -> (names that gate on sight, policy for
    #: the calls no name list can contain).
    #:
    #: Precomputed by the agent runtime and passed in, rather than resolved in
    #: `tools_node`, because working out a level's gate set needs the run's
    #: toolbox — `review` means "every tool *this agent* has" — and the toolbox
    #: is an agent concept this module knows nothing about. Left None (chat,
    #: and any caller that has not opted in), a mid-run switch is ignored and
    #: the fixed `sensitive_tools` / `approval_policy` above stand.
    approval_modes: dict[str, tuple[frozenset[str], ApprovalPolicy]] | None = None

    #: Called after every tool call finishes, with keywords:
    #: `call_id`, `name`, `args`, `output`, `status` ('completed' | 'failed'),
    #: `duration_ms`, `iteration`, `thought`. The agent runtime uses it to write
    #: one `AgentStep` row and broadcast a `node_complete` frame per call, which
    #: is what lets the canvas render an agent run. `AGENT_TRACE` alone cannot do
    #: that job: it fires *before* dispatch, so it knows neither the result nor
    #: whether the call succeeded.
    on_tool_result: ToolObserver | None = None

    #: Called once per model call, with keywords: `index` (1-based), `reasoning`
    #: (the model's thinking for *this* turn alone), `content`, `decision`
    #: ('tools' | 'answer'), `provider`, `model_id`, `tokens`, `duration_ms`.
    #: The agent runtime uses it to write one `AgentTurn` row, which is what
    #: gives every subsequent tool call something to belong to.
    #:
    #: The turn is the honest unit of an agent's work: calls issued in the same
    #: turn were decided together and their results all return to the *next*
    #: turn, never to each other. Recording it was previously left to a
    #: `{iteration, thought}` blob on each step, so the grouping could not be
    #: queried and the reasoning was a 150-character slice of it.
    on_model_turn: TurnObserver | None = None

    #: What `curate_node` is allowed to remove from the transcript when the run
    #: grows past its window. The default is disabled, so chat is untouched:
    #: chat's history is already bounded by `HISTORY_WINDOW` and its long
    #: answers by `context_summary`, and its transcript is one turn deep. The
    #: agent runtime builds a real policy from `SubAgent.runtime_settings`,
    #: where the three context-lifecycle toggles live — a long run is the case
    #: where the transcript, not the conversation, is what overflows.
    curation: Any = None

    #: Called once per curation pass, with keywords: `results_compacted`,
    #: `steps_folded`, `tokens_before`, `tokens_after`, `summary_tokens`,
    #: `archived_ids`. The agent runtime records it as a turn and streams it, so
    #: a user reading the run can see that the transcript was cut and by how
    #: much. Curation that leaves no trace is indistinguishable from a model
    #: that quietly forgot.
    on_curation: TurnObserver | None = None

    #: Whether a paused call is recorded in the HITL approval queue by this
    #: run's observer, which then owns telling the user about it.
    #:
    #: Set by the agent runtime; chat leaves it False because a chat turn has no
    #: `ExecutionLog` to hang a `HITLRequest` on — and needs none, since the
    #: person who typed the message is watching the stream that carries the
    #: prompt. Without the flag both paths would notify: `_require_approval`
    #: would write its own ad-hoc row *and* the queue's escalation ladder would
    #: write another, so one pause would reach the Inbox twice and one of the
    #: two would ignore the agent's `notifyOnHitl` setting entirely.
    approval_queue: bool = False

    @property
    def max_tokens(self) -> int:
        return _MAX_TOKENS_BY_INTENT.get(self.intent, _DEFAULT_MAX_TOKENS)


def iteration_limit(intent: str) -> int:
    """Bounded but generous tool-iteration cap for the given intent."""
    if intent in ("research", "search"):
        return min(40, max(MAX_TOOL_ITERATIONS * 3, 24))
    return min(30, max(MAX_TOOL_ITERATIONS * 2, 12))


class AgentState(TypedDict):
    """Only what changes during the run. Everything static is on the config."""

    messages: Annotated[list[BaseMessage], add_messages]
    metadata: dict[str, Any]
    tool_trace: list[dict[str, Any]]
    thinking: str
    total_tokens: int


def _context(config: RunnableConfig | None) -> TurnContext:
    turn = (config or {}).get("configurable", {}).get("turn")
    if not isinstance(turn, TurnContext):
        raise RuntimeError("Agent invoked without a TurnContext on its config.")
    return turn


# ── Message threading ────────────────────────────────────────────────────────

def to_wire(messages: Sequence[BaseMessage]) -> list[dict[str, Any]]:
    """
    Render LangChain messages as OpenAI-shaped dicts.

    Assistant turns keep their `tool_calls` and tool results keep their
    `tool_call_id`; that linkage is the whole point.
    """
    wire: list[dict[str, Any]] = []
    for message in messages:
        match message:
            case HumanMessage():
                wire.append({"role": "user", "content": message.content})
            case AIMessage():
                entry: dict[str, Any] = {
                    "role": "assistant",
                    "content": message.content or None,
                }
                if message.tool_calls:
                    entry["tool_calls"] = [
                        {
                            "id": call["id"],
                            "type": "function",
                            "function": {
                                "name": call["name"],
                                "arguments": json.dumps(call.get("args") or {}),
                            },
                        }
                        for call in message.tool_calls
                    ]
                wire.append(entry)
            case ToolMessage():
                wire.append({
                    "role": "tool",
                    "tool_call_id": message.tool_call_id,
                    "content": str(message.content),
                })
            case SystemMessage():
                # Only the curator puts one of these in state, and it has to
                # reach the model: a summary of the work that was removed is
                # worthless if it is dropped on the way out, and the model would
                # then see a run that simply forgot its first thirty steps.
                wire.append({"role": "system", "content": str(message.content)})
    return wire


def _split_transcript(
    messages: Sequence[BaseMessage], *, at_limit: bool, out_of_time: bool = False
) -> tuple[list[dict[str, Any]], str]:
    """
    Split the turn transcript into (wire history, trailing prompt).

    Every provider handler builds its request as `[system] + history + [user
    prompt]` — the prompt is always appended last. So when the transcript ends
    on tool output we hand the whole thing over as history and make the required
    trailing user turn a continuation instruction, which is useful anyway. When
    it ends on the user's own message we peel that off as the prompt.
    """
    if messages and isinstance(messages[-1], HumanMessage):
        return to_wire(messages[:-1]), str(messages[-1].content)

    if out_of_time:
        nudge = prompts.CONTINUE_OUT_OF_TIME
    elif at_limit:
        nudge = prompts.CONTINUE_AT_LIMIT
    else:
        nudge = prompts.CONTINUE
    return to_wire(messages), nudge


def _turn_number(messages: Sequence[BaseMessage]) -> int:
    return sum(1 for m in messages if isinstance(m, AIMessage))


# ── Vision / attachments ─────────────────────────────────────────────────────

#: Substring hints for models absent from the AIModel registry (new OpenRouter
#: entries, mostly). The registry is authoritative; this only avoids silently
#: dropping images for a model nobody has catalogued yet.
_VISION_HINTS = (
    "vision", "-vl", "gpt-4o", "gpt-5", "gemini", "claude-", "grok-4",
    "llama-4", "pixtral", "qwen-vl", "llava", "kimi",
)


async def supports_vision(model: str, provider: str) -> bool:
    """Whether `model` accepts image input."""
    from llm.models import AIModel

    entry = await AIModel.objects.filter(value=model, provider__slug=provider).afirst()
    if entry is not None:
        return entry.supports_image_input

    lowered = model.lower()
    guessed = any(hint in lowered for hint in _VISION_HINTS)
    logger.debug("[Vision] %s not in registry; hint match=%s", model, guessed)
    return guessed


async def _describe_attachment_for_text_model(attachment, *, witness: bool) -> str:
    """Render one attachment as text for a model that cannot see it.

    With a witness available this is a pointer rather than an apology. The
    difference matters: the old text ended the matter, so the model said "I
    cannot see it" and stopped, when a model that *can* see it was one tool call
    away the whole time.
    """
    if attachment.file_type in ("image", "video"):
        if witness and attachment.file_type == "image":
            return (
                f"### Attachment: {attachment.filename} (image, id {attachment.id})\n"
                f"[You cannot see this image yourself. Call ask_vision with "
                f"attachment_id \"{attachment.id}\" to question an assistant that "
                f"can. Ask specific questions; ask follow-ups when an answer is "
                f"vague. Its replies are testimony, not your own observation.]"
            )
        return (
            f"### Attachment: {attachment.filename} ({attachment.file_type})\n"
            f"[This model has no visual input, so you cannot see this file. "
            f"Say so rather than guessing at its contents.]"
        )

    text = getattr(attachment, "extracted_text", "") or ""
    if not text:
        from inference.utils import extract_text_from_file

        path = getattr(attachment.file, "path", None) or attachment.file.name
        try:
            text = await asyncio.to_thread(
                extract_text_from_file, path, attachment.file_type
            )
        except (OSError, ValueError) as exc:
            logger.warning("[Attachments] Cannot read %s: %s", attachment.filename, exc)
            text = ""

    body = text[:20_000] if text else "[Content could not be extracted.]"
    return f"### Attachment: {attachment.filename}\n{body}"


async def prepare_attachments(
    attachments: Sequence[Any], *, model: str, provider: str,
    user_id: int | None = None,
) -> tuple[tuple[Any, ...], str]:
    """
    Split attachments into (files passed to the model, text appended to prompt).

    Vision models get the files. Text-only models get extracted text instead, so
    an upload is never silently ignored.
    """
    if not attachments:
        return (), ""

    if await supports_vision(model, provider):
        return tuple(attachments), ""

    from chat.vision import witness_available

    witness = await witness_available(user_id)
    described = [
        await _describe_attachment_for_text_model(a, witness=witness)
        for a in attachments
    ]
    return (), "\n\n## Uploaded files\n" + "\n\n".join(described)


# ── Agent node ───────────────────────────────────────────────────────────────

async def _run_model(
    turn: TurnContext,
    *,
    prompt: str,
    history: list[dict[str, Any]],
    tools: list[dict] | None,
) -> Completion:
    """
    Call the model, streaming content to the sink as it arrives.

    Content is emitted live and retracted with CONTENT_RESET if the response
    turns out to be a preamble to a tool call. Streaming optimistically and
    correcting the rare case beats withholding every answer until we know.
    """
    accumulator = StreamAccumulator()
    try:
        async with asyncio.timeout(LLM_CALL_TIMEOUT):
            async for chunk in llm.stream(
                provider=turn.provider,
                model=turn.model,
                prompt=prompt,
                system_message=turn.system_message,
                user_id=turn.user_id,
                temperature=turn.temperature,
                max_tokens=turn.max_tokens,
                tools=tools,
                history=history,
                attachments=list(turn.attachments),
            ):
                match accumulator.add(chunk):
                    case "content" if not accumulator.has_tool_calls:
                        await turn.sink(
                            Event.CONTENT_CHUNK, {"content": chunk.get("content", "")}
                        )
                    case "thinking":
                        await turn.sink(
                            Event.THINKING_CHUNK, {"content": chunk.get("content", "")}
                        )
                    case "error":
                        logger.error("[Agent] Provider error: %s", accumulator.error)
                        break
    except asyncio.TimeoutError:
        logger.warning("[Agent] Model stream exceeded %ss", LLM_CALL_TIMEOUT)
        if not accumulator.content:
            accumulator.error = "The model took too long to respond."

    completion = accumulator.finish()

    if completion.tool_calls and completion.content:
        # What we streamed was preamble, not the answer. Retract it and keep it
        # as reasoning so the work is visible but not mistaken for the reply.
        await turn.sink(Event.CONTENT_RESET, {})
        return Completion(
            content="",
            thinking=f"{completion.thinking}\n{completion.content}".strip(),
            tool_calls=completion.tool_calls,
            usage=completion.usage,
            tokens=completion.tokens,
        )

    # An out-of-credit key, a rejected key, or a model the provider has retired
    # is the user's to fix, so it is raised and reported as an error rather than
    # shown as something the assistant said. The raw provider body is never a
    # good answer either way.
    actionable = accumulator.actionable_error(turn.provider, turn.model)
    if actionable is not None and not completion.content:
        raise actionable

    if accumulator.error and not completion.content:
        # Everything else — an outage, a malformed request — still reaches the
        # user, but as a sentence rather than as the provider's JSON. The full
        # body is in the log line above for whoever has to debug it.
        return Completion(
            content=f"⚠️ {humanize_provider_body(accumulator.error)}",
            usage=completion.usage,
            tokens=completion.tokens,
        )

    return completion


async def agent_node(state: AgentState, config: RunnableConfig) -> dict[str, Any]:
    """One model turn: answer, or ask for tools."""
    turn = _context(config)
    iteration = _turn_number(state["messages"])
    # Two ways to reach the last pass, one mechanism. Running out of *steps* and
    # running out of *time* both mean "answer now with what you have", and both
    # are served by withholding tools below — a run stopped any other way has
    # paid for every tool call it made and returns none of what they found.
    out_of_time = turn.deadline is not None and turn.deadline.wrapping_up
    at_limit = iteration >= turn.max_iterations - 1 or out_of_time

    await turn.sink(Event.STATUS, {
        "phase": "thinking",
        "message": (
            "Wrapping up — time limit reached..." if out_of_time
            else "Thinking..." if not iteration
            else f"Reasoning (step {iteration + 1})..."
        ),
    })

    prior, prompt = _split_transcript(
        state["messages"], at_limit=at_limit, out_of_time=out_of_time,
    )
    history = list(turn.history) + prior

    # Withholding tools on the last permitted iteration is what forces an answer
    # instead of a loop that runs out of budget mid-tool-call.
    tools = None
    if not at_limit:
        from chat import tools as tool_registry

        tools = await (
            turn.tool_source()
            if turn.tool_source is not None
            else tool_registry.get_available_tools(
                turn.user_id,
                memory_enabled=turn.memory_enabled,
                session_key=turn.session_id,
            )
        )

    started = time.monotonic()
    try:
        completion = await _run_model(turn, prompt=prompt, history=history, tools=tools)
    except LLMUserActionable:
        # Deliberately not caught: no credential, no credit, or a model that no
        # longer exists. No retry and no rephrasing helps. It ends the turn as
        # an error frame the client shows as such, instead of an apology in the
        # assistant's voice that reads like the model chose not to answer.
        raise
    except LLMUnavailable as exc:
        logger.warning("[Agent] %s", exc)
        completion = Completion(content=_NO_PROVIDER_MESSAGE)
    except Exception:
        logger.exception("[Agent] Model call failed")
        completion = Completion(
            content="Something went wrong reaching the model. Please try again."
        )

    calls, content = completion.tool_calls, completion.content or ""
    if not calls and content and not at_limit:
        calls, content = await _recover_text_tool_calls(content, turn)

    message = AIMessage(
        content=content,
        tool_calls=[
            {"name": c.name, "args": c.arguments, "id": c.id} for c in calls
        ],
    )

    thinking = state.get("thinking", "")
    if completion.thinking:
        thinking = f"{thinking}\n\n{completion.thinking}".strip()

    if turn.on_model_turn is not None:
        # `completion.thinking`, never the accumulated `thinking`: the observer
        # writes one row per turn, and handing it the running total would make
        # each turn's reasoning a superset of the last -- growing quadratically
        # and attributing turn 1's thoughts to turn 7.
        #
        # Never allowed to break the turn. A run must not fail because
        # something was watching it, which is the same rule `on_tool_result`
        # keeps for the same reason.
        try:
            await turn.on_model_turn(
                index=iteration + 1,
                reasoning=completion.thinking or "",
                content=content,
                decision="tools" if calls else "answer",
                provider=turn.provider,
                model_id=turn.model,
                tokens=completion.tokens,
                usage=completion.usage,
                duration_ms=int((time.monotonic() - started) * 1000),
            )
        except Exception:  # noqa: BLE001
            logger.exception("[Agent] on_model_turn observer raised")

    return {
        "messages": [message],
        "thinking": thinking,
        "total_tokens": state.get("total_tokens", 0) + completion.tokens,
    }


async def _recover_text_tool_calls(
    content: str, turn: TurnContext
) -> tuple[tuple[ToolCall, ...], str]:
    """
    Last-resort parse of tool calls a weak model wrote as text.

    SOTA models emit native `tool_calls` and never reach this; it exists for
    small local models that describe the call in prose. Returns the calls and
    the message with their raw syntax removed, so the user never sees it.
    """
    from .extraction import split_text_tool_calls

    calls, cleaned = split_text_tool_calls(content)
    if not calls:
        return (), content

    logger.info("[Agent] Recovered %d text-form tool call(s)", len(calls))
    # That text already went out as content chunks; retract it like any preamble.
    await turn.sink(Event.CONTENT_RESET, {})
    return calls, cleaned


# ── Tool node ────────────────────────────────────────────────────────────────

async def _collect_media(
    result: dict, meta: dict, sink: EventSink, *, key: str, event: Event
) -> None:
    """Append `key` items from a tool result onto metadata and notify the client."""
    items = result.get(key) or []
    if not items:
        return
    merged = [*meta.get(key, []), *items]
    meta[key] = merged
    await sink(event, {key: merged})


async def _on_web_search(
    result: dict, args: dict, meta: dict, sink: EventSink
) -> None:
    if result.get("type") != "search_results":
        return

    by_url = {source.get("url"): source for source in meta.get("sources", [])}
    for source in result.get("sources", []):
        by_url.setdefault(source.get("url"), source)
    meta["sources"] = list(by_url.values())[:50]
    meta["search_query"] = args.get("query", "")
    await sink(Event.SOURCES_UPDATE, {"sources": meta["sources"]})

    # A web search also fills the image strip. The model rarely calls
    # image_search of its own accord, and a Perplexity-style answer with an
    # empty visual panel reads as broken rather than as restraint.
    if query := args.get("query"):
        from chat.sources.search import image_search

        try:
            await _collect_media(
                {"images": await image_search(query)}, meta, sink,
                key="images", event=Event.IMAGES_UPDATE,
            )
        except Exception:
            logger.warning("[Tools] Companion image search failed", exc_info=True)


async def _on_image_search(
    result: dict, args: dict, meta: dict, sink: EventSink
) -> None:
    await _collect_media(result, meta, sink, key="images", event=Event.IMAGES_UPDATE)


async def _on_video_search(
    result: dict, args: dict, meta: dict, sink: EventSink
) -> None:
    await _collect_media(result, meta, sink, key="videos", event=Event.VIDEOS_UPDATE)


async def _on_artifact(
    result: dict, args: dict, meta: dict, sink: EventSink
) -> None:
    if result.get("type") != "html_artifact":
        return
    artifact = {k: result.get(k) for k in ("title", "html", "width", "height")}
    meta.setdefault("html_artifacts", []).append(artifact)
    await sink(Event.HTML_ARTIFACT, artifact)


async def _on_kb_search(
    result: dict, args: dict, meta: dict, sink: EventSink
) -> None:
    if result.get("status") != "success":
        return
    meta["kb_search_results"] = result.get("results", [])
    await sink(Event.STATUS, {
        "phase": "rag_results",
        "message": f"Found {result.get('count', 0)} relevant document chunks.",
    })


async def _on_scrape(
    result: dict, args: dict, meta: dict, sink: EventSink
) -> None:
    if result.get("status") != "success":
        return
    await sink(Event.STATUS, {
        "phase": "page_scraped",
        "message": f"Read {result.get('url', 'the page')}.",
    })


async def _on_history_search(
    result: dict, args: dict, meta: dict, sink: EventSink
) -> None:
    # Surfaced so recalling something from 200 turns ago reads as retrieval
    # rather than the model simply having had it in context.
    if not result.get("matches"):
        return
    await sink(Event.STATUS, {
        "phase": "history_recall",
        "message": f"Recalled {result.get('returned', 0)} earlier message(s).",
    })


async def _on_deep_research(
    result: dict, args: dict, meta: dict, sink: EventSink
) -> None:
    if result.get("sources"):
        meta["sources"] = result["sources"][:50]
        await sink(Event.SOURCES_UPDATE, {"sources": meta["sources"]})
    meta["search_queries"] = result.get("queries", [])

    # Same reasoning as the companion image search: research answers carry a
    # visual panel, and the topic is the right query for it.
    if topic := args.get("topic"):
        from chat.sources.search import image_search, video_search

        try:
            await _collect_media({"images": await image_search(topic)}, meta, sink,
                                 key="images", event=Event.IMAGES_UPDATE)
            await _collect_media({"videos": await video_search(topic)}, meta, sink,
                                 key="videos", event=Event.VIDEOS_UPDATE)
        except Exception:
            logger.warning("[Tools] Companion media search failed", exc_info=True)


#: tool name → side effect applied to metadata / streamed to the client.
#: The model always gets the raw tool output regardless; these only drive the UI.
_SIDE_EFFECTS = {
    "web_search": _on_web_search,
    "image_search": _on_image_search,
    "video_search": _on_video_search,
    "render_html_artifact": _on_artifact,
    "knowledge_base_search": _on_kb_search,
    "scrape_webpage": _on_scrape,
    "search_conversation_history": _on_history_search,
    "deep_research": _on_deep_research,
}


async def _apply_side_effects(
    name: str, args: dict, raw_result: str, meta: dict, sink: EventSink
) -> None:
    handler = _SIDE_EFFECTS.get(name)
    if handler is None:
        return
    try:
        parsed = json.loads(raw_result)
    except (json.JSONDecodeError, TypeError):
        return  # tool returned prose; nothing structured to surface
    if not isinstance(parsed, dict):
        return
    try:
        await handler(parsed, args, meta, sink)
    except Exception:
        # A UI side effect must never fail the turn — the model already has the
        # result, which is what actually answers the user.
        logger.exception("[Tools] Side effect for %s failed", name)


async def _require_approval(call: ToolCall, turn: TurnContext, meta: dict) -> None:
    """Pause the graph until the user approves a sensitive tool call."""
    if call.id in meta.get("approved_tool_calls", []):
        return

    logger.info("[Tools] Pausing for approval of %s", call.name)
    # An agent run queues the pause as a `HITLRequest` instead (see
    # `agents/agent/hitl.py`), and the reminder ladder hanging off that row is
    # what notifies — honouring the agent's `notifyOnHitl`, the user's device
    # preference and their quiet hours, none of which this call knows about.
    # Writing one here as well would put the same pause in the Inbox twice.
    if not turn.approval_queue:
        try:
            from asgiref.sync import sync_to_async
            from django.contrib.auth import get_user_model
            from notifications.utils import create_notification

            @sync_to_async
            def notify() -> None:
                create_notification(
                    user=get_user_model().objects.get(id=turn.user_id),
                    type="hitl_request",
                    title="Permission required",
                    message=f"The assistant wants to run: {call.name}",
                    data={"tool": call.name, "args": call.arguments,
                          "thread_id": turn.session_id},
                )

            await notify()
        except Exception:
            logger.exception("[Tools] HITL notification failed")

    await turn.sink(Event.ASK_PERMISSION, {
        "tool": call.name, "args": call.arguments, "call_id": call.id,
    })
    interrupt(f"Permission required for {call.name}")


def _refusal_text(name: str, reason: str) -> str:
    """What the model is told when the user declines a call it asked for."""
    reason = (reason or "").strip()
    base = f"The user declined to run {name}."
    if reason:
        base = f"{base} Reason: {reason}"
    return (
        f"{base} Do not retry it. Continue with what you can do without it, "
        f"or explain what you now cannot do."
    )


async def tools_node(state: AgentState, config: RunnableConfig) -> dict[str, Any]:
    """Execute every tool the last assistant turn asked for."""
    from chat.tools import permissions
    from chat.tools import tool_output
    from chat import tools as tool_registry

    turn = _context(config)
    last = state["messages"][-1]
    if not isinstance(last, AIMessage) or not last.tool_calls:
        return {"messages": []}

    meta = dict(state.get("metadata", {}))
    trace = list(state.get("tool_trace", []))
    iteration = _turn_number(state["messages"])
    reasoning = (state.get("thinking") or "").strip()[-150:]
    results: list[ToolMessage] = []

    tool_context = {
        "user_id": turn.user_id,
        "session_id": turn.session_id,
        "turn_id": turn.turn_id,
        "depth": turn.depth,
        # The run's own clock, so a tool that starts *other* runs can bound
        # them by what is left of it. Only `invoke_subagent` reads it; every
        # other tool is bounded by its own timeout and by the loop stopping.
        "deadline": turn.deadline,
        # None in chat. The file tools read it to find the subtree they may
        # address, and answer "no file access" rather than guessing a default —
        # a default here would be a scope nobody granted.
        "file_scope": turn.file_scope,
        # Same shape, same reasoning, for the knowledge bases: the KB tools
        # filter on it rather than resolving anything the user owns.
        "kb_scope": turn.kb_scope,
        # Extra archives the retrieval tools may read — a worker's parent.
        "archive_scopes": turn.archive_scopes,
    }
    # Per-call, filled in just before dispatch below. A tool that starts other
    # runs (`invoke_subagent`, `run_agent`) needs to name the step that invoked
    # it, so the worker's log can point back at the exact tool call — and
    # through it at the reasoning that chose to delegate.
    sensitive = (
        turn.sensitive_tools
        if turn.sensitive_tools is not None
        else frozenset(tool_registry.SENSITIVE_TOOLS)
    )
    dispatch = turn.tool_dispatch or tool_registry.execute_tool
    policy = turn.approval_policy or permissions.default_policy

    # A user watching the run may have loosened (or tightened) how much it asks
    # since the last batch. Read here, per batch, rather than captured with the
    # rest of the turn: `TurnContext` is frozen, and a mode that could only be
    # chosen before the run started is the thing this is fixing. The override
    # is not drained — it stands for the rest of the run.
    if turn.approval_modes:
        from . import steering

        chosen = steering.autonomy(turn.session_id)
        if chosen and chosen in turn.approval_modes:
            sensitive, policy = turn.approval_modes[chosen]

    calls = [
        ToolCall(id=raw["id"], name=raw["name"], arguments=dict(raw.get("args") or {}))
        for raw in last.tool_calls
    ]
    rejected: dict[str, str] = dict(meta.get("rejected_tool_calls", {}) or {})

    # ── Pass 1: settle permission for every call before dispatching any ──
    #
    # This runs as its own pass rather than inline with dispatch because
    # `interrupt()` discards the node's writes and re-runs it from the top on
    # resume. Interleaved, a batch of [safe, sensitive] would dispatch the safe
    # call, pause on the sensitive one, and then dispatch the safe one *a
    # second time* when the user approved — sending the email twice, writing a
    # second `AgentStep` row, and re-firing the UI side effects. Graph
    # state is rolled back by the interrupt; the outside world is not.
    #
    # Settling every permission first makes the node's re-run idempotent: the
    # only work before the pause is asking, and the answers persist in
    # `metadata` (written by `approve_tool_call` / `reject_tool_call` from
    # outside the node, so they survive the rollback).
    for call in calls:
        if call.id in rejected:
            continue
        # Two gates, checked cheapest first. The name list carries reasoning
        # about tools we wrote; the policy inspects calls nobody could have
        # listed in advance. Either one is enough to pause.
        if call.name in sensitive or await policy(call.name, call.arguments, tool_context):
            await _require_approval(call, turn, meta)

    # ── Pass 2: plan every call, in call order ──
    #
    # Arguments, trace entries and the AGENT_TRACE frames are all built here,
    # before anything is dispatched, so what the UI is told never depends on
    # which tool happens to finish first.
    planned: list[tuple[Any, dict]] = []
    for call in calls:
        refusal = rejected.get(call.id)
        if refusal is not None:
            # A declined call still owes the model a `tool` message: the
            # assistant turn requested it by id, and a transcript with a
            # dangling `tool_call_id` is malformed. Answering with the refusal
            # is also what lets the model adapt — before this, a rejection left
            # the graph paused for ever, because nothing ever resumed it.
            trace.append({"tool": call.name, "args": call.arguments,
                          "iteration": iteration, "thought": reasoning,
                          "summary": "declined by user", "call_id": call.id,
                          "status": "rejected"})
            continue

        # web_search with no query is the one omission worth repairing rather
        # than bouncing back — the user's own message is always the right query.
        arguments = dict(call.arguments)
        if call.name == "web_search" and not arguments.get("query"):
            arguments["query"] = turn.user_text

        entry = {"tool": call.name, "args": arguments, "iteration": iteration,
                 "thought": reasoning, "summary": reasoning, "call_id": call.id}
        trace.append(entry)
        await turn.sink(Event.AGENT_TRACE, {"sub_type": "tool", **entry})
        planned.append((call, arguments))

    async def _dispatch_one(call, arguments) -> tuple[str, str, int]:
        """Run one call. Returns (output, status, duration_ms); never raises."""
        # Its own copy of the context. `call_id` used to be written onto the
        # single shared dict immediately before each dispatch, which is exactly
        # the field a concurrent sibling would overwrite — and
        # `invoke_subagent` reads it to record which tool call spawned a
        # worker, so a race there misattributes whole runs.
        ctx = {**tool_context, "call_id": call.id}
        started = time.monotonic()
        try:
            async with asyncio.timeout(TOOL_CALL_TIMEOUT):
                output = await dispatch(call.name, arguments, ctx)
            status = "completed"
        except asyncio.TimeoutError:
            status = "failed"
            output = f"Error: {call.name} timed out after {TOOL_CALL_TIMEOUT}s."
        except Exception as exc:  # noqa: BLE001
            logger.exception("[Tools] %s raised", call.name)
            status = "failed"
            output = f"Error running {call.name}: {exc}"
        return output, status, int((time.monotonic() - started) * 1000)

    # ── Pass 3: dispatch — the read-only calls together, the rest one by one ──
    #
    # A model issues every call in a turn before seeing any result, so nothing
    # in this batch can depend on anything else in it and overlapping is safe
    # by construction. Only for tools that say so: `PARALLEL_TOOLS` is an
    # allow-list, so an unknown name — every MCP tool, since theirs are minted
    # at runtime — stays serial. Sensitive calls are excluded whatever they
    # declare, because a tool worth pausing a human for is a tool with a side
    # effect, and two of those in one turn may well be ordered.
    def _may_overlap(call) -> bool:
        return call.name in tool_registry.PARALLEL_TOOLS and call.name not in sensitive

    concurrent = [(c, a) for c, a in planned if _may_overlap(c)]
    serial = [(c, a) for c, a in planned if not _may_overlap(c)]

    outcomes: dict[str, tuple[str, str, int]] = {}
    if len(concurrent) > 1:
        logger.info("[Tools] iter=%d dispatching %d calls in parallel: %s",
                    iteration, len(concurrent), [c.name for c, _ in concurrent])
        gathered = await asyncio.gather(
            *(_dispatch_one(c, a) for c, a in concurrent)
        )
        outcomes.update({c.id: o for (c, _), o in zip(concurrent, gathered)})
    else:
        serial = concurrent + serial      # a lone call gains nothing from gather

    for call, arguments in serial:
        logger.info("[Tools] iter=%d %s(%s)", iteration, call.name, sorted(arguments))
        outcomes[call.id] = await _dispatch_one(call, arguments)

    # ── Pass 4: observe and record, in call order ──
    #
    # Deliberately not inside the dispatch above. `_apply_side_effects` does a
    # read-modify-write on the shared `meta` (see `_collect_media`), and the
    # observer writes one `AgentStep` row per call — doing either in completion
    # order would make the transcript, the step rows and the UI's search
    # results reshuffle between runs of the same turn.
    args_by_id = {call.id: arguments for call, arguments in planned}
    for call in calls:
        refusal = rejected.get(call.id)
        if refusal is not None:
            results.append(ToolMessage(
                content=_refusal_text(call.name, refusal),
                tool_call_id=call.id, name=call.name,
            ))
            continue

        output, status, duration_ms = outcomes[call.id]
        arguments = args_by_id[call.id]

        if turn.on_tool_result is not None:
            # Never let an observer break a tool call that already succeeded —
            # it exists to watch the run, not to take part in it.
            try:
                await turn.on_tool_result(
                    call_id=call.id, name=call.name, args=arguments,
                    output=output, status=status, duration_ms=duration_ms,
                    iteration=iteration, thought=reasoning,
                )
            except Exception:  # noqa: BLE001
                logger.exception("[Tools] on_tool_result observer raised")

        await _apply_side_effects(call.name, arguments, output, meta, turn.sink)

        # Bounded here and nowhere earlier: the observer above wants the whole
        # result for its durable log, and the side effects parse it as JSON to
        # drive the UI. Both are done with it by this point, so the only reader
        # left is the model — which is the one that has to pay for every
        # character. Anything trimmed is stored and named in what comes back.
        model_output = await tool_output.bound(
            call.name, output, {**tool_context, "call_id": call.id})
        results.append(
            ToolMessage(content=model_output, tool_call_id=call.id, name=call.name)
        )

    return {"messages": results, "metadata": meta, "tool_trace": trace}


async def steering_node(state: AgentState, config: RunnableConfig) -> dict[str, Any]:
    """Pick up anything the user said while the run was working.

    A plain node, deliberately. `interrupt()` exists to stop and wait for an
    actor outside the graph; a steer is already in the mailbox by the time this
    runs, so there is nothing to wait for — and using an interrupt here would
    give `run_turn`'s pause detection a second reason to fire that it would
    then have to tell apart from an approval.

    Costs one dict lookup per tool round when nobody is steering.
    """
    from . import steering

    turn = _context(config)
    message = steering.take(turn.session_id)
    if not message:
        return {}

    logger.info("[Steer] Delivering a steer into %s", turn.session_id)
    await turn.sink(Event.STATUS, {
        "phase": "steered",
        "message": "Picking up your message...",
    })
    # A user message, not a system one: it is the user talking, and the model
    # already knows how to weigh a later instruction against an earlier one.
    return {"messages": [HumanMessage(content=message)]}


# ── Curation node ────────────────────────────────────────────────────────────

async def _summariser_for(turn: TurnContext):
    """The `async (text) -> (summary, tokens)` the curator folds with.

    A pinned cheap model by default rather than the run's own: a forty-turn run
    on an expensive model would otherwise pay full rate to compress itself, and
    the fold is an extractive job that does not need the model the user chose
    the agent for. Falls back to the run's model when nothing is pinned, because
    a fold that cannot run at all is worse than one that costs a little.
    """
    from . import curation

    policy = turn.curation
    provider = policy.summary_provider or turn.provider
    model = policy.summary_model or turn.model

    async def _call(provider_: str, model_: str, text: str) -> tuple[str, int]:
        completion = await llm.complete(
            provider=provider_,
            model=model_,
            prompt=text,
            system_message=curation.SUMMARY_INSTRUCTION,
            user_id=turn.user_id,
            temperature=0,
            max_tokens=1_024,
        )
        return completion.content or "", completion.tokens

    async def summarise(text: str) -> tuple[str, int]:
        try:
            return await _call(provider, model, text)
        except LLMUserActionable:
            # No credential for the pinned model, no credit, or it has been
            # retired. The run's own model is known to work — this turn has been
            # using it — so falling back keeps the note rather than losing the
            # steps behind it. Only worth trying when it is a *different* call.
            if (provider, model) == (turn.provider, turn.model):
                raise
            logger.warning(
                "[Curation] Fold model %s/%s unusable; folding with the run's own",
                provider, model,
            )
            return await _call(turn.provider, turn.model, text)

    return summarise


async def curate_node(state: AgentState, config: RunnableConfig) -> dict[str, Any]:
    """Cut the transcript back when it has grown past the model's window.

    A node rather than a step inside `agent_node` because the cut has to reach
    graph state: replacements carry the ids they replace, so `add_messages`
    substitutes them in the checkpoint and the next turn starts from the curated
    transcript. Doing it on the outgoing copy would re-do — and re-archive — the
    same work on every remaining turn of the run.

    It sits on the tools -> agent path because that is the only place the
    transcript grows. Returning `{}` when there is nothing to do is the common
    case and costs one token estimate.
    """
    turn = _context(config)
    if turn.curation is None or not getattr(turn.curation, "enabled", False):
        return {}

    from . import curation

    try:
        result = await curation.curate(
            state["messages"],
            policy=turn.curation,
            model=turn.model,
            reserve_output=turn.max_tokens,
            # The system message and any conversation outside the run are
            # already spoken for; the watermark has to be measured against the
            # whole request, not against the part that lives in graph state.
            baseline_tokens=(
                llm.estimate_tokens(turn.system_message)
                + sum(llm.estimate_tokens(str(e.get("content") or "")) for e in turn.history)
            ),
            context={
                "user_id": turn.user_id,
                "session_id": turn.session_id,
                "turn_id": turn.turn_id,
            },
            summarise=await _summariser_for(turn),
        )
    except Exception:  # noqa: BLE001
        # Curation is a cost control, not a correctness one — `clamp_input` is
        # still behind it. A run must never fail because the thing that keeps it
        # cheap broke.
        logger.exception("[Agent] Curation failed; transcript left as it was")
        return {}

    if not result.curated:
        return {}

    await turn.sink(Event.STATUS, {
        "phase": "curating",
        "message": "Condensing earlier steps to stay inside the context window...",
    })

    if turn.on_curation is not None:
        try:
            await turn.on_curation(
                results_compacted=result.results_compacted,
                steps_folded=result.steps_folded,
                tokens_before=result.tokens_before,
                tokens_after=result.tokens_after,
                summary_tokens=result.summary_tokens,
                archived_ids=result.archived_ids,
            )
        except Exception:  # noqa: BLE001
            logger.exception("[Agent] on_curation observer raised")

    return {
        "messages": result.updates,
        # The fold is a real model call and is charged for like one: it counts
        # against the run's token total, and therefore against the spend cap.
        # A summariser that spent money invisibly would be a hole in the
        # guardrail it is meant to serve.
        "total_tokens": state.get("total_tokens", 0) + result.summary_tokens,
    }


# ── Graph ────────────────────────────────────────────────────────────────────

def _next_step(state: AgentState) -> str:
    last = state["messages"][-1]
    return "tools" if isinstance(last, AIMessage) and last.tool_calls else END


def _build_graph():
    graph = StateGraph(AgentState)
    graph.add_node("agent", agent_node)
    graph.add_node("tools", tools_node)
    graph.add_node("steering", steering_node)
    graph.add_node("curate", curate_node)
    graph.set_entry_point("agent")
    graph.add_conditional_edges("agent", _next_step, {"tools": "tools", END: END})
    # tools -> steering -> agent, rather than tools -> agent. The steer lands
    # *after* the tool results are threaded and *before* the model reads them,
    # which is the only boundary where a new user message cannot separate an
    # assistant turn from the `tool` messages answering its call ids.
    # Curation goes between them, not after: it is the tool results that make a
    # transcript outgrow its window, so this is the only edge where cutting is
    # ever needed — and it must run *before* the steer so the steer stays the
    # last message in state. `_split_transcript` peels a trailing `HumanMessage`
    # off as the turn's prompt, which is exactly where a new instruction
    # belongs; a summary note appended after it would take that place instead.
    graph.add_edge("tools", "curate")
    graph.add_edge("curate", "steering")
    graph.add_edge("steering", "agent")
    return graph.compile(checkpointer=MemorySaver())


chat_agent_graph = _build_graph()


async def forget_thread(thread_id: str) -> bool:
    """Drop one thread's checkpoints from the in-process saver.

    `MemorySaver` has no eviction: no `maxsize`, no TTL, nothing that ever
    expires. Every super-step of every run it has ever checkpointed stays
    resident for the life of the process, and a run's transcript grows
    quadratically in its own iteration count — so the process grows without
    bound while nothing is leaking in the ordinary sense. Agent runs made it
    sharpest, because each one gets a fresh uuid thread id and every fanout
    worker gets another: once a run is finished, its thread can never be
    reached again, and nothing was deleting it.

    Only ever call this on a run that has actually ended. A *paused* run is
    exactly the case that needs its checkpoint kept — the approval resumes from
    it, and dropping it would strand the run the user is being asked about.

    Best-effort by design: failing to free memory must not fail a run that has
    already produced its answer.
    """
    try:
        checkpointer = getattr(chat_agent_graph, "checkpointer", None)
        deleter = getattr(checkpointer, "adelete_thread", None)
        if deleter is None:
            return False
        await deleter(thread_id)
        return True
    except Exception:  # noqa: BLE001
        logger.warning("[Agent] Could not drop checkpoints for %s", thread_id,
                       exc_info=True)
        return False


# ── Public API ───────────────────────────────────────────────────────────────

@dataclass(slots=True)
class TurnResult:
    """What one agent turn produced."""

    answer: str = ""
    thinking: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    tool_trace: list[dict[str, Any]] = field(default_factory=list)
    tokens: int = 0
    #: True when the run paused for tool approval rather than finishing.
    awaiting_approval: bool = False


async def run_tool_eagerly(
    name: str,
    arguments: dict[str, Any],
    *,
    turn: TurnContext,
    metadata: dict[str, Any],
) -> tuple[str, dict[str, Any]]:
    """
    Run one tool before the model's first turn, applying its UI side effects.

    Used when the user has *explicitly* asked for something — `/search`,
    `/research` — where leaving it to the model to decide is a regression: they
    chose the mode, so the search must happen. Ordinary chat stays fully
    model-driven. Returns the tool output and its trace entry.
    """
    from chat import tools as tool_registry

    await turn.sink(Event.AGENT_TRACE, {
        "sub_type": "tool", "tool": name, "args": arguments, "iteration": 0,
    })

    try:
        async with asyncio.timeout(TOOL_CALL_TIMEOUT):
            output = await tool_registry.execute_tool(
                name, arguments,
                {"user_id": turn.user_id, "session_id": turn.session_id,
                 "turn_id": turn.turn_id},
            )
    except asyncio.TimeoutError:
        output = f"Error: {name} timed out after {TOOL_CALL_TIMEOUT}s."
    except Exception as exc:
        logger.exception("[Tools] Eager %s raised", name)
        output = f"Error running {name}: {exc}"

    await _apply_side_effects(name, arguments, output, metadata, turn.sink)
    return output, {"tool": name, "args": arguments, "iteration": 0}


def _pending_tool_name(snapshot, call_id: str) -> str | None:
    """Which tool the paused call was for, read back out of graph state."""
    for message in reversed(snapshot.values.get("messages", [])):
        if not isinstance(message, AIMessage):
            continue
        for raw in message.tool_calls or []:
            if raw.get("id") == call_id:
                return raw.get("name")
    return None


async def approve_tool_call(
    thread_id: str, call_id: str, *, remember: bool = False,
    scope: str = "", session_key: str = "", user_id: int | None = None,
) -> None:
    """
    Record a user's approval so the paused run can resume past it.

    `scope` says how long the approval lasts, and it is the whole reason this
    is not a boolean any more:

    * `once` — this call only. The default, and the only answer that cannot be
      regretted.
    * `session` — this tool, for the rest of this conversation or run. Written
      against `ToolPermission.session_key`, a column that has existed since the
      model was added and that nothing ever wrote a non-empty value into.
    * `always` — this tool, in every conversation, until revoked.

    Before this there were two rungs, `once` and `always`, and the button
    offering the second said "remember". A user who wants to stop being asked
    about the next twenty calls in the run they are watching had no way to say
    that, so they said `always` and granted a standing allowance over their own
    mailbox to get through the afternoon. The middle rung is the one people
    actually want, and it expires on its own.

    Still keyed on the tool rather than on this call's arguments: a decision the
    user could only make about one exact argument set would never match twice,
    and they would keep answering the same prompt while believing they had
    settled it. Narrowing it further needs a way to *show* the user what they
    are agreeing to, which is a question about the prompt, not about storage.

    `remember` is the retired spelling of `scope='always'`, kept because
    `chat/turn/pipeline.py` speaks it over the wire. `scope` wins when both
    are given.

    `session_key` is what a `session`-scoped allowance is filed under, and it
    has to be passed rather than taken from `thread_id` because the two are the
    same string only *sometimes*. An agent run uses its thread id as its
    session id, so they agree; a chat turn with memory off gets a throwaway
    thread (`<id>:nomem:<uuid>`) while `TurnContext.session_id` stays the real
    session id — and `permissions.is_remembered` matches against the latter. Key
    it on the thread there and the row is written, matches nothing, and the user
    is asked again having been told they would not be.
    """
    scope = scope or ("always" if remember else "once")
    if scope not in ("once", "session", "always"):
        logger.warning("[HITL] Unknown approval scope %r; treating as once", scope)
        scope = "once"
    config: RunnableConfig = {"configurable": {"thread_id": thread_id}}
    snapshot = await chat_agent_graph.aget_state(config)
    if not snapshot.values:
        logger.warning("[HITL] No state for thread %s; approval ignored", thread_id)
        return

    meta = dict(snapshot.values.get("metadata", {}))
    approved = list(meta.get("approved_tool_calls", []))
    if call_id not in approved:
        approved.append(call_id)
    meta["approved_tool_calls"] = approved
    await chat_agent_graph.aupdate_state(config, {"metadata": meta})

    if scope == "once" or user_id is None:
        return

    tool_name = _pending_tool_name(snapshot, call_id)
    if not tool_name:
        logger.warning("[HITL] Cannot remember %s: no pending call by that id", call_id)
        return

    from chat.models import ToolPermission

    # `session` scopes the allowance to the run or conversation the user is
    # actually watching; `always` leaves the key empty, which is what
    # `permissions.is_remembered` matches in every session.
    stored_key = (session_key or thread_id)[:64] if scope == "session" else ""

    try:
        await ToolPermission.objects.aget_or_create(
            user_id=user_id, tool_name=tool_name[:160], session_key=stored_key,
        )
    except Exception:  # noqa: BLE001
        # The approval itself already landed. Failing to file it must not undo
        # the resume the user actually asked for.
        logger.exception("[HITL] Could not store the standing allowance")
    logger.info("[HITL] Approved %s on thread %s", call_id, thread_id)


async def reject_tool_call(
    thread_id: str, call_id: str, *, reason: str = "",
) -> bool:
    """
    Record a refusal so the paused run can resume *past* the call it asked for.

    Approval and rejection are deliberately the same mechanism — a note in
    `metadata`, written from outside the graph so it survives the interrupt's
    rollback — because they are the same decision with opposite answers. What
    they must not be is asymmetric in effect: before this existed, approving
    resumed the run and rejecting did nothing at all, so a declined call left
    the graph paused for ever. With a schedule or a trigger behind it that is a
    run that never ends and a `HITLRequest` that nudges the user for ever.

    Returns False when there is no state for the thread, so a caller can tell
    "declined" from "there was nothing to decline".
    """
    config: RunnableConfig = {"configurable": {"thread_id": thread_id}}
    snapshot = await chat_agent_graph.aget_state(config)
    if not snapshot.values:
        logger.warning("[HITL] No state for thread %s; rejection ignored", thread_id)
        return False

    meta = dict(snapshot.values.get("metadata", {}))
    rejected = dict(meta.get("rejected_tool_calls", {}) or {})
    rejected[call_id] = reason
    meta["rejected_tool_calls"] = rejected
    await chat_agent_graph.aupdate_state(config, {"metadata": meta})
    logger.info("[HITL] Rejected %s on thread %s", call_id, thread_id)
    return True


async def run_turn(
    turn: TurnContext,
    *,
    prompt: str,
    thread_id: str,
    metadata: dict[str, Any] | None = None,
    tool_trace: list[dict[str, Any]] | None = None,
) -> TurnResult:
    """
    Run the agent to completion (or to an approval pause) and return the result.

    `thread_id` keys the checkpointer. Callers wanting a turn with no recall
    should pass a throwaway id: the checkpoint holds its own copy of the
    conversation, so emptying `turn.history` alone would not stop the model
    seeing earlier turns.
    """
    config: RunnableConfig = {
        "configurable": {"thread_id": thread_id, "turn": turn},
        "recursion_limit": turn.max_iterations * 2 + 10,
    }
    initial: AgentState = {
        "messages": [HumanMessage(content=prompt)],
        "metadata": dict(metadata or {}),
        "tool_trace": list(tool_trace or []),
        "thinking": "",
        "total_tokens": 0,
    }

    # Resume only when the graph is genuinely paused mid-run. `.values` alone is
    # the wrong test: thread_id is the session id, so every turn after the first
    # finds leftover values and would resume a finished graph — re-emitting the
    # previous answer and never reading the new message. `.next` is non-empty
    # only while nodes are still pending.
    snapshot = await chat_agent_graph.aget_state(config)
    resuming = bool(snapshot.values and snapshot.next)

    awaiting_approval = False
    try:
        final = await chat_agent_graph.ainvoke(None if resuming else initial, config=config)
    except LLMUserActionable:
        # No credential, rejected key, no credit, or a retired model. The caller
        # turns this into an error the user sees as an error; swallowing it into
        # "I hit an internal error" below would hide the one thing they can act
        # on.
        raise
    except GraphInterrupt:
        # Pre-1.0 LangGraph surfaced a pause by raising out of `ainvoke`. Kept
        # so the pause is detected under either version.
        awaiting_approval = True
        final = (await chat_agent_graph.aget_state(config)).values
    except Exception:
        logger.exception("[Agent] Run failed on thread %s", thread_id)
        return TurnResult(
            answer="I hit an internal error on that turn. Please try again.",
            metadata=dict(metadata or {}),
        )
    else:
        # LangGraph 1.x does *not* raise: it returns the state with the pending
        # pause reported in `__interrupt__`. Detecting the pause by matching
        # "Permission required" against an exception message therefore stopped
        # working at the 1.0 upgrade, silently — the graph paused correctly and
        # `run_turn` reported the turn complete, so `run_agent` closed the log
        # as `completed`, `_find_paused_log` found nothing, and approving did
        # nothing at all. Read it from the result, which is where it now lives.
        awaiting_approval = bool(final.get("__interrupt__"))

    answer = next(
        (m.content for m in reversed(final["messages"])
         if isinstance(m, AIMessage) and m.content),
        "",
    )
    return TurnResult(
        answer=answer,
        thinking=final.get("thinking", ""),
        metadata=final.get("metadata", dict(metadata or {})),
        tool_trace=final.get("tool_trace", []),
        tokens=final.get("total_tokens", 0),
        awaiting_approval=awaiting_approval,
    )


async def suggest_follow_ups(
    turn: TurnContext, *, question: str, answer: str, limit: int = 3
) -> list[str]:
    """
    Ask for follow-up questions in a separate, cheap call.

    Deliberately not a field on the main answer. Requiring the model to wrap a
    long markdown reply in JSON just to carry three questions is what made the
    answer impossible to stream token-by-token; this costs one small call after
    the user is already reading.
    """
    if len(answer.strip()) < 200:
        return []

    try:
        completion = await llm.complete(
            provider=turn.provider,
            model=turn.model,
            prompt=prompts.FOLLOW_UPS_TEMPLATE.format(
                question=question[:2_000], answer=answer[:6_000]
            ),
            system_message=prompts.FOLLOW_UPS_SYSTEM,
            user_id=turn.user_id,
            max_tokens=300,
            temperature=0.8,
        )
    except (LLMUnavailable, RuntimeError) as exc:
        logger.info("[FollowUps] Skipped: %s", exc)
        return []

    return _parse_follow_ups(completion.content, limit)


def _parse_follow_ups(raw: str, limit: int) -> list[str]:
    """Pull the questions out of a small JSON reply; empty list if it is junk."""
    start, end = raw.find("{"), raw.rfind("}")
    if start == -1 or end <= start:
        return []
    try:
        data = json.loads(raw[start:end + 1])
    except json.JSONDecodeError:
        return []

    items = data.get("follow_ups") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return []
    return [str(item).strip() for item in items if str(item).strip()][:limit]
