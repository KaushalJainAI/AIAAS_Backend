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
from dataclasses import dataclass, field
from typing import Annotated, Any, Sequence, TypedDict

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, StateGraph
from langgraph.graph.message import add_messages
from langgraph.types import interrupt

from workflow_backend.thresholds import MAX_TOOL_ITERATIONS

from . import llm, prompts
from .events import Event, EventSink, null_sink
from .llm import Completion, LLMUnavailable, StreamAccumulator, ToolCall

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
    return wire


def _split_transcript(
    messages: Sequence[BaseMessage], *, at_limit: bool
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

    nudge = prompts.CONTINUE_AT_LIMIT if at_limit else prompts.CONTINUE
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
    from nodes.models import AIModel

    entry = await AIModel.objects.filter(value=model, provider__slug=provider).afirst()
    if entry is not None:
        return entry.supports_image_input

    lowered = model.lower()
    guessed = any(hint in lowered for hint in _VISION_HINTS)
    logger.debug("[Vision] %s not in registry; hint match=%s", model, guessed)
    return guessed


async def _describe_attachment_for_text_model(attachment) -> str:
    """Render one attachment as text for a model that cannot see it."""
    if attachment.file_type in ("image", "video"):
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
    attachments: Sequence[Any], *, model: str, provider: str
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

    described = [await _describe_attachment_for_text_model(a) for a in attachments]
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
            tokens=completion.tokens,
        )

    if accumulator.error and not completion.content:
        return Completion(content=f"⚠️ {accumulator.error}", tokens=completion.tokens)

    return completion


async def agent_node(state: AgentState, config: RunnableConfig) -> dict[str, Any]:
    """One model turn: answer, or ask for tools."""
    turn = _context(config)
    iteration = _turn_number(state["messages"])
    at_limit = iteration >= turn.max_iterations - 1

    await turn.sink(Event.STATUS, {
        "phase": "thinking",
        "message": "Thinking..." if not iteration else f"Reasoning (step {iteration + 1})...",
    })

    prior, prompt = _split_transcript(state["messages"], at_limit=at_limit)
    history = list(turn.history) + prior

    # Withholding tools on the last permitted iteration is what forces an answer
    # instead of a loop that runs out of budget mid-tool-call.
    tools = None
    if not at_limit:
        from . import tools as tool_registry

        tools = await tool_registry.get_available_tools(
            turn.user_id, memory_enabled=turn.memory_enabled
        )

    try:
        completion = await _run_model(turn, prompt=prompt, history=history, tools=tools)
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
        from .search import image_search

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
        from .search import image_search, video_search

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
    try:
        from asgiref.sync import sync_to_async
        from core.models import User
        from notifications.utils import create_notification

        @sync_to_async
        def notify() -> None:
            create_notification(
                user=User.objects.get(id=turn.user_id),
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


async def tools_node(state: AgentState, config: RunnableConfig) -> dict[str, Any]:
    """Execute every tool the last assistant turn asked for."""
    from . import tools as tool_registry

    turn = _context(config)
    last = state["messages"][-1]
    if not isinstance(last, AIMessage) or not last.tool_calls:
        return {"messages": []}

    meta = dict(state.get("metadata", {}))
    trace = list(state.get("tool_trace", []))
    iteration = _turn_number(state["messages"])
    reasoning = (state.get("thinking") or "").strip()[-150:]
    results: list[ToolMessage] = []

    tool_context = {"user_id": turn.user_id, "session_id": turn.session_id}

    for raw in last.tool_calls:
        call = ToolCall(
            id=raw["id"], name=raw["name"], arguments=dict(raw.get("args") or {})
        )

        if call.name in tool_registry.SENSITIVE_TOOLS:
            await _require_approval(call, turn, meta)

        # web_search with no query is the one omission worth repairing rather
        # than bouncing back — the user's own message is always the right query.
        arguments = dict(call.arguments)
        if call.name == "web_search" and not arguments.get("query"):
            arguments["query"] = turn.user_text

        entry = {"tool": call.name, "args": arguments, "iteration": iteration,
                 "thought": reasoning, "summary": reasoning}
        trace.append(entry)
        await turn.sink(Event.AGENT_TRACE, {"sub_type": "tool", **entry})

        logger.info("[Tools] iter=%d %s(%s)", iteration, call.name, sorted(arguments))
        try:
            async with asyncio.timeout(TOOL_CALL_TIMEOUT):
                output = await tool_registry.execute_tool(
                    call.name, arguments, tool_context
                )
        except asyncio.TimeoutError:
            output = f"Error: {call.name} timed out after {TOOL_CALL_TIMEOUT}s."
        except Exception as exc:
            logger.exception("[Tools] %s raised", call.name)
            output = f"Error running {call.name}: {exc}"

        await _apply_side_effects(call.name, arguments, output, meta, turn.sink)
        results.append(
            ToolMessage(content=str(output), tool_call_id=call.id, name=call.name)
        )

    return {"messages": results, "metadata": meta, "tool_trace": trace}


# ── Graph ────────────────────────────────────────────────────────────────────

def _next_step(state: AgentState) -> str:
    last = state["messages"][-1]
    return "tools" if isinstance(last, AIMessage) and last.tool_calls else END


def _build_graph():
    graph = StateGraph(AgentState)
    graph.add_node("agent", agent_node)
    graph.add_node("tools", tools_node)
    graph.set_entry_point("agent")
    graph.add_conditional_edges("agent", _next_step, {"tools": "tools", END: END})
    graph.add_edge("tools", "agent")
    return graph.compile(checkpointer=MemorySaver())


chat_agent_graph = _build_graph()


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
    from . import tools as tool_registry

    await turn.sink(Event.AGENT_TRACE, {
        "sub_type": "tool", "tool": name, "args": arguments, "iteration": 0,
    })

    try:
        async with asyncio.timeout(TOOL_CALL_TIMEOUT):
            output = await tool_registry.execute_tool(
                name, arguments, {"user_id": turn.user_id, "session_id": turn.session_id}
            )
    except asyncio.TimeoutError:
        output = f"Error: {name} timed out after {TOOL_CALL_TIMEOUT}s."
    except Exception as exc:
        logger.exception("[Tools] Eager %s raised", name)
        output = f"Error running {name}: {exc}"

    await _apply_side_effects(name, arguments, output, metadata, turn.sink)
    return output, {"tool": name, "args": arguments, "iteration": 0}


async def approve_tool_call(thread_id: str, call_id: str) -> None:
    """Record a user's approval so the paused run can resume past it."""
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
    logger.info("[HITL] Approved %s on thread %s", call_id, thread_id)


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
    except Exception as exc:
        if "Permission required" not in str(exc):
            logger.exception("[Agent] Run failed on thread %s", thread_id)
            return TurnResult(
                answer="I hit an internal error on that turn. Please try again.",
                metadata=dict(metadata or {}),
            )
        awaiting_approval = True
        final = (await chat_agent_graph.aget_state(config)).values

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
