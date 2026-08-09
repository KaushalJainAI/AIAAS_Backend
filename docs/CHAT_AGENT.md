# Chat Agent

The standalone conversational agent in `Backend/chat/`. Where the
`KingOrchestrator` (`Backend/orchestrator/`) runs deterministic multi-step DAG
workflows, this is the free-form chat surface: ask a question, get an answer,
with tools used as needed.

## Module map

| Module | Responsibility |
|---|---|
| `views.py` | HTTP only — authenticate, parse, delegate, serialise |
| `pipeline.py` | One turn end to end; both endpoints run exactly this |
| `agent.py` | The LangGraph tool loop, and the turn's public API |
| `llm.py` | Provider access: routing, credentials, budgets, stream folding |
| `tools.py` | Tool schemas and implementations |
| `search.py` | Web/image/video search and page fetching |
| `history.py` | What the model gets to see this turn |
| `prompts.py` | System prompt and the auxiliary prompts |
| `events.py` / `sse.py` | The event contract, and its SSE transport |
| `extraction.py` | Fallback parser for models without native tool calls |

## The central design decision

**The model receives a real transcript.** An assistant turn that requested tools
goes back as an assistant message carrying `tool_calls`; each result goes back as
a `tool` message with the matching `tool_call_id`.

This is load-bearing, not stylistic. The previous implementation flattened tool
results into prose:

```text
--- PREVIOUS ACTIONS & TOOL RESULTS IN THIS TURN ---
Assistant Action: ...
Tool 'web_search' Result: ...
```

That takes the model off the distribution it was trained on, and it responds by
*imitating* tool-call syntax in text rather than emitting it. Compensating for
that required a 480-line scraper with twenty regexes covering a dozen invented
dialects. Thread the messages correctly and native tool calls simply work.

Providers accept this because every handler in `nodes/handlers/llm_nodes.py`
builds its request as `[system] + history + [user prompt]` and extends `history`
verbatim. `agent.to_wire` renders the LangChain messages into that shape.

One consequence: the trailing slot is always a user message, so when the
transcript ends on tool output the whole transcript goes in as history and the
trailing turn carries a continuation instruction (`prompts.CONTINUE`).

## Response format: plain markdown

The agent streams markdown. There is no JSON envelope.

The earlier contract required every reply to arrive as
`{"response": ..., "summary": ..., "follow_ups": [...]}`, which meant nothing
could be shown until the whole object had arrived and parsed — `content_chunk`
was commented out in the view. Around 550 lines existed to coax, repair and
re-parse that JSON, including a second LLM call whose only job was fixing
malformed output.

Instead:

- **The answer** streams token-by-token as `content_chunk`.
- **Follow-up questions** come from one small separate call after the answer
  (`agent.suggest_follow_ups`), skipped for short replies.
- **The summary** is derived deterministically from the answer
  (`pipeline.context_summary`) — it feeds later context windows, so it never
  needed to be the model's job.

### Streaming and preambles

Content streams optimistically. If the response turns out to have requested a
tool as well — a model saying "let me look that up" before calling
`web_search` — the agent emits `content_reset` and the client clears its live
buffer. Retracting the rare preamble beats withholding every answer until the
response is known to be final.

## Turn flow

```
views.send_message_stream
  └─ pipeline.run_chat_turn(sink=SSEBridge.sink)
       ├─ classify intent, guard media capability
       ├─ persist the user message
       ├─ history.load_history → to_wire_history
       ├─ history.partition_attachments  (blocked uploads are reported, not dropped)
       ├─ prompts.build_system_message
       ├─ eager recall injection (see below)
       ├─ agent.run_turn ──> LangGraph: agent ⇄ tools
       ├─ agent.suggest_follow_ups
       └─ persist the assistant message (+ summary, sources, trace)
```

`send_message` is the same call with an event sink that discards.

## Behaviours worth knowing

**Memory toggle.** `session.memory_enabled` gates *recall*, not *retention* —
messages are always written to the database. With it off the turn also gets a
throwaway checkpointer thread id, because the graph keeps its own copy of the
conversation keyed by `thread_id`; emptying the history payload alone would not
stop the model reading earlier turns back out of the checkpoint.

**Eager recall.** When the user plainly refers to something said earlier, the
pipeline runs `search_conversation_history` itself and injects the result. The
model has the tool and the prompt tells it to use it, but smaller models
measurably did not — answering "I don't have that in my context" with an empty
tool trace. Recall is the feature, so it cannot depend on the model choosing to
call a function.

**Attachments.** Vision models receive files directly. Text-only models get
extracted text instead, so an upload is never silently ignored. Files that no
model can read are reported to the user *and* to the model, which stops it
answering as though it had seen them.

**Deep research** is a tool (`deep_research`), not a mode: it plans queries,
searches, reads pages and returns the corpus with its sources. It used to run
ahead of the model on a keyword guess; as a tool the model decides when the
question warrants it and can follow up on what comes back.

**Human-in-the-loop.** Tools in `SENSITIVE_TOOLS` pause the graph via
`interrupt()` and emit `ask_permission`. The client resumes by posting
`approve_tool_call`. `/api/chat/execute-tool/` refuses these outright — reaching
them there would be a way around the gate rather than a shortcut to it.

**Token budgets.** `llm.clamp_input` runs on the assembled request, the first
point the real total is known. History is dropped oldest-first because it is the
only recoverable part — it stays in the database and
`search_conversation_history` can fetch it back — and the model is told when
this happened so it reaches for retrieval instead of assuming the gap is empty.

**Weak models.** `extraction.py` recovers tool calls written as text, in two
shapes only. Capable models never reach it. Guessing at more exotic formats
would risk running the *wrong* tool, which is worse than telling the model its
call was not understood.

## Events

`events.Event` is the contract; the values are the names the frontend switches
on.

| Event | Meaning |
|---|---|
| `status` | Phase change (`planning`, `thinking`, `memory_off`, `history_recall`, …) |
| `thinking_chunk` | Reasoning tokens |
| `content_chunk` | Answer tokens |
| `content_reset` | Streamed text was a preamble; clear the buffer |
| `agent_trace` | A tool call, with its arguments |
| `sources_update` / `images_update` / `videos_update` | Collected media |
| `html_artifact` | Rendered artifact |
| `attachments_blocked` | Uploads the model cannot read |
| `ask_permission` | HITL gate |
| `done` | Final user + assistant messages |
| `error` | Turn failed |

## Testing

- `tests_pipeline.py` — full turns with the provider faked at `llm.stream`:
  streaming, tool threading, memory toggle, failure handling.
- `tests_units.py` — stream folding, tool-call normalisation, the text fallback.
- `tests_rework.py` — context budgets, transcript threading, tool registry.
- `tests.py` — the guest pipeline.

Fake at `chat.llm.stream` and stub `chat.tools.get_available_tools`; the real
tool list reaches out to the user's MCP servers.
