# Chat Agent

How the chat assistant in `Backend/chat/` works: what happens to one message,
the AI loop at its centre, and the design choices a newcomer would otherwise
undo by accident.

Saved agents (`agents/`) run the **same loop**. The difference is only in
configuration: which tools, which safety level, who is watching. So once you
understand this page you understand how agents run too.

## Where things are

| Path | What it does |
|---|---|
| `chat/views.py` | HTTP only: check who you are, read the request, hand it on, send the answer back |
| `chat/turn/runs.py` | Keeps a turn running after the HTTP request that started it, so a browser refresh can reattach |
| `chat/turn/pipeline.py` | **One turn, end to end** (`run_chat_turn`). Read this first |
| `chat/turn/agent.py` | **The AI loop** (a LangGraph graph) and the settings a run carries (`TurnContext`) |
| `chat/turn/prompts.py` | The system prompt and the small prompts used inside the loop |
| `chat/turn/history.py` | What the model gets to see from earlier in the conversation |
| `chat/turn/curation.py` | Shrinking a long run so it fits the model's memory |
| `chat/turn/steering.py` | Messages you send while a run is working |
| `chat/turn/todos.py` | The run's own plan (its todo list) |
| `chat/turn/reviewer.py` | In `auto` mode, a second model decides whether a risky call needs you |
| `chat/turn/events.py` | The list of events streamed to the browser |
| `chat/turn/extraction.py` | Backup parser for weak models that write tool calls as plain text |
| `chat/transport/` | Turning events into a streamed HTTP response (server-sent events) |
| `chat/sources/` | Web search and file attachments |
| `chat/tools/` | Every tool. One file per topic; each tool is declared once with `@tool(...)` |
| `chat/commands/` | Slash commands like `/agent` and `/memory` |
| `chat/vision/` | Lets a text-only model ask questions about an image |
| `chat/guest/` | Chat without an account |
| `llm/access.py` | Not in this app, but every model call goes through it |

## One message, step by step

```
POST /api/chat/sessions/<id>/message/stream/
  chat/views.py  send_message_stream
    └─ chat/turn/runs.py  start           (the turn now lives outside the request)
        └─ chat/turn/pipeline.py  run_chat_turn
             1. preflight: is there a working model key? Fail now if not.
             2. slash command? resolve it (chat/commands/)
             3. save your message
             4. load history, your saved memories, attachments
             5. build the system prompt + a "context update" message
             6. maybe search first (web search / earlier messages)
             7. chat/turn/agent.py  run_turn   ← the AI loop, below
             8. suggest follow-up questions (a small separate model call)
             9. save the answer, with sources, cost and trace
  events stream back as "data:" lines the whole time
```

`send_message` (the non-streaming endpoint) runs the exact same function and
throws the events away.

## The AI loop

`chat/turn/agent.py::_build_graph` builds a loop of four steps:

```
agent ──► tools ──► curate ──► steering ──► agent ... (until a final answer)
```

1. **agent**: send the conversation to the model. It either answers, or asks
   for one or more tools.
2. **tools**: run those tools. Safe ones (`parallel=True`) run at the same time.
   Risky ones may pause for your approval first.
3. **curate**: if the conversation is getting too long for the model, shrink
   the oldest part (see `CONTEXT_LIFECYCLE.md`).
4. **steering**: if you typed something while it worked, add it now.

The loop stops when the model answers without asking for a tool, or when it
hits its iteration limit. At the limit it is told to answer with what it has,
so a long run ends with a partial answer instead of an error.

## Design choices to keep

**The model gets a real transcript.** When the model asks for a tool, that
request goes back to it as an assistant message with `tool_calls`, and each
result goes back as a `tool` message with the matching id. An older version
pasted tool results in as prose. Models then started *writing* fake tool calls
in text instead of making real ones, and it took a 480-line regex scraper to
cope. Keep the real format and native tool calls just work.

**The answer streams as plain markdown.** There is no JSON wrapper. Text is
shown as soon as it arrives (`content_chunk`). If the model says "let me look
that up" and *then* calls a tool, the server sends `content_reset` and the
browser clears that preamble. Follow-up questions come from a small separate
call after the answer (`suggest_follow_ups`).

**The system prompt only holds things that never change during a session.**
The clock, and anything else that changes every turn, goes into a separate
"context update" message near the end. If the system prompt changed every
turn, the provider could never reuse its cache of the prompt, and every turn
would cost more. Your saved memories are the one exception: they change rarely.

**Memory off means no earlier messages, not "don't save".** Messages are always
saved. With memory off the model just does not see earlier turns. The turn also
gets a throwaway `thread_id`, because LangGraph keeps its own copy of the
conversation keyed by that id.

**Recall doesn't wait for the model.** When you plainly refer to something said
earlier, the pipeline searches the conversation itself and adds the result.
Smaller models often didn't call the search tool on their own.

**Attachments are never silently ignored.** Vision models get the file. Text
models get its extracted text, or ask the vision helper (`ask_vision`).
Files nobody can read are reported to you *and* to the model.

**Approval.** A risky tool pauses the graph with LangGraph's `interrupt()` and
sends `ask_permission`. The browser answers with `approve_tool_call` or
`reject_tool_call`. All approvals are decided *before* any tool in the batch
runs, because `interrupt()` re-runs the whole step on resume, and a tool that
already sent an email would send it again. Which calls need approval is decided
in `chat/tools/permissions.py`.

**Every tool must check ownership itself.** `/api/chat/execute-tool/` lets a
logged-in user call a tool directly with any arguments. So "the model would
never ask for someone else's id" is not a security check. Each tool must check
that the thing it touches belongs to the caller.

**Delegating to a saved agent.** `run_agent` starts the agent run in the
background and waits for it, instead of running it inside the chat loop. The
chat loop cancels slow tool calls, and that would kill the agent halfway
through. Agents cannot call `run_agent` themselves: it is in no `GRANT_TOOLS`
group, so an agent cannot start an endless chain of agents.

**Weak models.** `extraction.py` recognises tool calls written as text in two
known shapes only. Guessing more formats risks running the *wrong* tool, which
is worse than telling the model it was not understood.

## Events sent to the browser

The full list is `chat/turn/events.py::Event`. The main ones:

| Event | Meaning |
|---|---|
| `status` | What stage the turn is at (`planning`, `thinking`, `memory_off`...) |
| `thinking_chunk` | The model's reasoning, as it arrives |
| `content_chunk` | The answer, as it arrives |
| `content_reset` | The text so far was a preamble; clear it |
| `agent_trace` | A tool call and its arguments |
| `sources_update`, `images_update`, `videos_update` | Search results to show |
| `chart`, `html_artifact` | Something to draw |
| `todos_update`, `files_update` | The run's plan, and files it wrote |
| `ask_permission` | Paused for your approval |
| `done` | The saved question and answer |
| `steers_returned` | Messages you sent too late for the run to read; put back in your input box |
| `error` | The turn failed |

On the frontend, `src/hooks/useChatStream.ts` turns these events into screen state.

## Testing

- `chat/tests/test_pipeline.py`: whole turns with the model faked. Fake it by
  patching `llm.access.stream`, and stub `chat.tools.get_available_tools`,
  because the real tool list tries to reach the user's MCP servers.
- `chat/tests/test_turn_output_e2e.py`: drives the real graph and checks the
  events and saved data a browser actually gets.
- `chat/tests/test_curation_e2e.py`: twenty real turns against a fake model,
  checking what was actually sent.
- `chat/tests/test_agent_tools.py`: the sandbox and each tool's ownership checks.
- `chat/tests/test_parallel_tools.py`, `test_steering.py`, `test_todos.py`,
  `test_permissions.py`: one feature each, as the names say.
