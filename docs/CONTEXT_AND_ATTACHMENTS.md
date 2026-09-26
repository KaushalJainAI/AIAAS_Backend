# Context and attachments: a simple guide

This page explains two things that belong together: what the model gets to see each turn (context), and how an attached file travels into that view (attachments).

Read it in order. The second half assumes the first.

---

## Part 1: Context

### 1. What context is

A model has a small desk. It can only see what is placed on it this turn.

Context is everything placed on the desk: your message, recent chat, file text, tool results, the time, the plan. If it does not fit, the oldest parts are shrunk or removed.

Nothing is ever deleted from the database to make room. Shrinking the desk does not erase the library. Old turns stay saved and can be searched.

### 2. What the model sees each turn

In order, from top to bottom:

1. **System baseline.** Who it is, the core rules, your saved facts about you. Same every turn of a session. Stable on purpose, so providers can cache it.
2. **Prior conversation.** The last 20 conversational turns (user + assistant). System rows inside that span ride along. Older turns are not replayed word for word.
3. **Attachment text.** Extracted text from recent uploads, folded into the messages that referenced them. Images go separately (see Part 2).
4. **Sources already reviewed.** Citations from earlier research turns. The last 3 keep snippets. Older ones become bare links.
5. **Context update.** What changed this turn: the clock, the mode nudge (research, image, video), files withheld from this model, the slash-command block, open todos. This rides as a trailing `system` message near the end, never inside the baseline. That keeps the top of the request byte-identical so caching still works.
6. **Your current message.** The question you just asked.
7. **Tool results this turn.** Each tool call and its answer, in matching pairs.

Then the model answers, or asks for tools. Tool calls run, results come back, and the loop repeats until a final answer or a limit.

### 3. Stable vs changing: why it matters

The baseline holds only what never changes mid-session: the session prompt, the core rules, the memory rule, your saved facts.

Everything that moves goes in the context update at the end: the time, the intent, blocked files, the command you typed.

Why: providers cache the start of a request. If the clock sat in the system prompt, the prefix would differ every turn and no cache could ever hit. Every turn would cost more and start slower. Your saved facts are the one exception — they change rarely, so they stay in the baseline.

### 4. Memory on, memory off

**Memory on** (default): the model sees the last 20 turns plus the extras above. To reach further back, it calls `search_conversation_history`. Replying "I don't have that" without searching first is treated as a failure.

**Memory off**: the model sees only your current message. Earlier turns are hidden, not deleted. Messages are still saved. Turn it back on and the history is still there. With memory off the turn also gets a throwaway id, so no loop state leaks between turns.

This gates recall, not retention.

### 5. Facts about you

Flat text, not a form. Examples: "prefers code first", "works in IST".

- An exact repeat touches the fact instead of adding a copy. Touched facts resist eviction.
- Caps are per category, so a burst of project facts cannot push out who you are.
- Eviction is least-recently-used. Rendering cuts on whole lines — half a sentence about someone can read as a fact and be flatly wrong.
- Agents read these facts but cannot write them. A scheduled run is personalised without rewriting the person while nobody watches.

Tools: `remember_about_user`, `forget_about_user`, `/memory`.

### 6. Plans, commands, sources

- **Todos.** The run's own plan (`update_todos`). Open items ride back every turn so a long job stays on track. Intent only, never results — items carrying output would be a second transcript.
- **Command block.** A slash command becomes one trailing context block for this turn (see `SLASH_COMMANDS.md`). Same slot as the clock: near the end, never the system prompt.
- **Charts and files.** What the run drew or wrote rides as structured data the UI renders (cards, panels), not as prose the model must re-read.

### 7. How big the desk is

| Limit | Value | What it means |
|---|---|---|
| History window | 20 turns | User + assistant turns replayed verbatim. System upload markers inside the span ride along. |
| Hard context | 100k tokens | Nothing assembled past this is sent. |
| Last-resort clamp | 96k tokens | Applied to the final payload, per model window. Counts tool arguments, not just text. Moves in whole segments (a tool call plus its answers, indivisible) so no `tool_call_id` points at nothing. |
| One message | 24k tokens | A single message may not eat the whole budget. |
| History search | 12 matches, 12k chars | The escape hatch is capped harder than the window it protects. |
| Tool result backstop | 64k chars | Per-tool ceilings sit below this (deep research 60k, `read_url` 15k, sandbox 20k). Over it, the full text spills to `ToolOutput` and the model gets a preview naming the id. A trimmed result and a short one never look alike. |

### 8. When it does not fit

**Chat** is short by design: 20 turns, then summaries plus `search_conversation_history` and `read_attachment_text`. No curation pass. Its transcript is one turn deep.

**Agent runs** can go 40 iterations with large tool results. Three large results alone can fill the budget. So agents curate at a watermark: nothing happens until ~70% full, then enough is cut to reach ~45% in one pass. Cutting a little every turn would rewrite the cached prefix every call.

Three mechanisms, weakest first:

1. **Compaction (free).** Old results shrink to a 400-char record keeping the call name and args. What a step did stays legible. What it returned becomes a pointer with an id.
2. **Fold (one cheap model call).** Oldest steps fold into exactly one running note (~250 words). The previous note is absorbed each time. There is never a second note. The fold model is your agent's `summaryModel` if set, else the platform default, else the run's own model. Its tokens count against the spend cap.
3. **Archive (what makes 1–2 safe).** Everything removed is stored first in `ToolOutput`. The record names the id. Tools `recall_context` and `read_tool_output` fetch it back. They are offered only once something is stored. With indexing off, the notice says the text is gone — it never names an id nobody wrote.

The last 3 segments are never touched. A model that cannot see what it just did repeats it.

What a run may pull in is also bounded: knowledge-base scope (which corpora), file scope (which folders), delegation caps (task length, briefing length, worker count — refused, not trimmed), one shared briefing as context not instructions, and read-through to the parent's archive for one hop.

Detail lives in `CONTEXT_LIFECYCLE.md`. This page is the simple version.

### 8b. KV cache hits: what we do from our side

Providers reuse work when the start of a request is byte-identical to the last one. That reused work is the KV cache. A hit means faster first token and cheaper prompt tokens. A miss means the whole prefix is re-read and re-billed.

From our side, everything below is about keeping that prefix stable. We do well here. Whether a given model actually hits also depends on the provider (see the honest note at the end).

1. **Stable head, moving tail.** The system baseline holds only what rarely changes: session prompt, core rules, memory rule, your saved facts. The clock, the intent nudge, blocked files, the slash-command block, and open todos ride as a trailing `system` message at the end, after history and before your question. The clock alone used to sit in the system prompt and guarantee a miss every turn. It no longer does. Test: `chat/tests/test_context.py`.
2. **Order is fixed.** System, then history, then the trailing update, then your message, then tool results. New facts land at the end so the head stays identical.
3. **Plans and commands never go up top.** Todos live in run state beside the messages and render as a trailing message each turn. A slash-command expansion joins the same trailing slot. Both change most turns, so putting them in the system prompt would cost the whole session's cache, not one call.
4. **Tools are declared once, natively.** Tool schemas travel in the `tools` field, not as prose in the prompt. No second copy drifts, and nothing advertises tools on the final pass when they are withheld to force an answer.
5. **Curation in one jump, not a trickle.** Agent runs curate only at ~70% full, then cut to ~45% in one pass. The last 3 segments are never touched, and the fold leaves exactly one note at the end. Shaving a little every turn would rewrite the prefix on every call.
6. **Hot-path reads are primed once per turn.** Context-window size, effort support, vision witness, and fallback model are fetched in `preflight()` and read from memory inside every model call. No extra database wait sits inside the request path. The approval reviewer also caches its verdict per call id, so a resumed turn never re-judges and never re-asks.
7. **We measure it.** Every model call logs `[Latency] ... prompt=X cached=Y(Z%)` from the provider's own usage object (`prompt_tokens_details.cached_tokens`). The split is stored on the message, the turn, and the run, and summed for cost. OpenRouter is asked to return its real charge plus the cache split, so billing reflects hits.

Honest note: the first turn of a session is always ~0% (a write). Later turns in the same session should climb. If `cached_read` stays 0 while the prefix is stable, the provider or route is not returning hits — for example the free router swapping its backing model. That is a provider fact, not a prefix bug. Check fast: grep `[Latency]` for one session's later turns, then compare `total_cached_read_tokens` on `/api/logs/`.

---

## Part 2: Attachments

### 9. How to attach a file

Upload a file to a chat session. The app:

1. Saves the bytes to disk (`chat_attachments/…`).
2. Classifies by extension: `image`, `pdf`, `pptx`, `docx`, `xlsx`, `text`, `other`.
3. Pulls the text out (see below). Caps stored text at 500k chars. Flags `is_large_file` past 120k chars.
4. Writes a system marker message in the conversation carrying `metadata.attachment_id`. This marker is how history finds the file later.
5. For `pdf`, `pptx`, and `text` with real text, registers a `Document` row and indexes it in the background for RAG search.
6. Returns a preview (first ~120k chars) so you can see what the model will start from.

Attach once, reference for the whole session. You do not need to re-upload next turn.

Supported for text reading today: `.png .jpg .jpeg .gif .webp .bmp` (as images), `.pdf`, `.pptx` / `.ppt`, `.docx` / `.doc`, `.xlsx` / `.xlsm` / `.xls`, `.txt .md .csv .json .xml .html .yml .yaml`. Anything else is `other`: bytes kept, text empty (see section 13).

Max upload is 50 MB. Office zips are checked against a 200 MB uncompressed ceiling before they are opened.

### 10. What the model actually receives

It depends on what the model can read. Sending an image to a text-only model is either a 400 error or a silent drop — both look like the assistant ignored you. So the app splits first:

- **Vision model:** gets the files directly.
- **Text-extracted types** (`pdf`, `pptx`, `docx`, `xlsx`, `text`): ride in as ordinary text tokens. No special model support needed.
- **Text-only model + image:** the file stays off the wire. The model gets a pointer with the attachment id and is told to call `ask_vision` (a second model that can see) instead of guessing. What comes back is testimony, not sight — the prompt forbids "I can see that…".
- **No reader at all:** reported to you and to the model, in different words. You get the plain reason plus whether switching models helps. The model gets the id (if any) and an instruction to say so rather than guess.

Only the last 5 turns contribute their uploads. Older uploads stay saved and searchable but stop riding automatically. Large files (`is_large_file`) are excluded from the auto set and go the RAG / on-demand route instead.

### 11. First full, then preview

Whether an old upload is re-injected in full depends on timing:

- **Not yet answered after:** the first time the model needs it, the full extracted text is pasted under the message (`FULL RESOURCE CONTENT`).
- **Already answered after:** later turns get a short preview plus the id and an instruction: "you already read this in full, call `read_attachment_text` with this id to quote it precisely."

This is why the model does not ask you to upload twice, and why long files do not eat the window every turn.

Three on-demand tools complete the loop:

- `read_attachment_text` — page a file's full text by id.
- `get_chat_message_full_text` — page a long assistant answer that was stored as a summary.
- `search_conversation_history` — grep older turns (12 matches, capped snippets) instead of replaying them.

Rule 6 of the core prompt says the same: older turns arrive as summaries, so quote or analyse via these tools rather than asking the user again.

### 12. RAG indexing: the second life of an upload

For `pdf`, `pptx`, and `text` uploads with real text, the app also creates an `inference.Document` and processes it in a background thread: chunk (~500 chars), embed, index into the session knowledge base.

That gives two paths to the same file:

- **In-context:** the pasted text above. Fast, exact, costs tokens.
- **Retrieval:** semantic search over the chunks. Cheap, survives the history window, works for large files.

Deleting a message with an attachment removes all three: the file on disk, the vector index entry, and the `Document` row. Deleting a session purges every attachment's outside rows too — cascading alone would orphan the `Document` rows and they would keep answering queries for a conversation that no longer exists.

### 13. Images, scans, and `other` files

- **Images** a text model cannot see go through the witness (`ask_vision`, max 6 questions per image per turn). Disagreement between two readers is surfaced, not hidden — the measured failure is a misread decimal that is silent and plausible.
- **Scanned PDFs** with no text layer extract to little or nothing. They behave like images: ask via the witness or switch to a vision model.
- **`other`** (zip, audio, binary, unknown): text is empty on purpose. Nothing is invented. The bytes are kept so `run_python_on_files` can open them. Unknown types default to `other` (never `txt`) so zip noise does not pollute the search index.
- **Legacy `.doc`** filed as `docx` returns empty rather than garbage. The model is told it cannot read it as text.

### 14. If the model says it cannot see your file

Check in order:

1. Is it inside the last 5 turns? Older uploads stop auto-attaching. Mention it again or call it by name so the model fetches it.
2. Is it large (>120k chars extracted)? It rides via preview + RAG, not full paste. Ask the model to quote via `read_attachment_text`.
3. Is it an image on a text-only model? Either switch to a multimodal model or tell it to use `ask_vision` with the file's id.
4. Is it `other` or a scan? The text may genuinely be empty. The UI preview length tells you — zero extracted chars means zero tokens to read.

---

## For developers: where it lives

- `Backend/chat/turn/prompts.py` — `build_system_message` (stable baseline) + `build_context_update` (volatile tail). Clock and command blocks live here, never up top.
- `Backend/chat/turn/history.py` — `load_history`, attachment + source enrichment, `to_wire_history`, `recent_attachments`, `partition_attachments` (sendable vs blocked, witness detour).
- `Backend/chat/turn/agent.py` — `prepare_attachments`, `supports_vision`, per-turn text rendering for text-only models.
- `Backend/chat/sources/attachments.py` — `classify_file`, extractors (`pdf`, `pptx`, `docx`, `xlsx`, `text`), `index_for_rag`, `release_attachment`, `purge_session`.
- `Backend/chat/views.py` — `upload_file` (classify, extract, cap, flag, marker message, index), delete paths that call `release_attachment`.
- `Backend/chat/models.py` — `ChatSession` (memory, model, autonomy), `ChatMessage` (content + metadata), `ChatAttachment` (file, type, `extracted_text`, `inference_document`, `is_large_file`), `ToolOutput`.
- `Backend/chat/turn/curation.py`, `llm/budget.py`, `chat/tools/tool_output.py` — the long-run path (watermark, segments, spill, recall). Full design in `CONTEXT_LIFECYCLE.md`.
- `Backend/workflow_backend/thresholds.py` — every number on this page (`HISTORY_WINDOW`, `MAX_CONTEXT_TOKENS`, `DOCUMENT_EXTRACT_CAP`, `IS_LARGE_FILE_THRESHOLD`, `LARGE_FILE_PREVIEW_LENGTH`, `TOOL_OUTPUT_CHAR_LIMIT`, watermark ratios).
- `Backend/chat/vision/` — the witness (`ask_vision`). Design and measurements in `VISION_AGENT.md`.
- Tests: `chat/tests/test_context.py` (baseline vs tail), `chat/tests/test_curation*.py` (long runs), `chat/tests/test_attachment_types.py`, `inference/tests/test_file_types.py` (uploads), `chat/tests/test_vision.py` (witness).
