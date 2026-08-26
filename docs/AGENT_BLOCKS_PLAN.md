# Agents from blocks — the ship plan

Written 2026-08-14. Supersedes the DAG-first parts of
`AGENT_WORKFLOW_MERGE_PLAN.md` and `WORKFLOW_RETIREMENT.md`.

**Goal:** stop building agents the n8n way. An agent becomes a *configuration
assembled from typed blocks*, compiled and validated before it runs, with a
diagnostics panel that says plainly when it cannot work. Scope is cut to what
ships.

---

## 1. The idea, and why it is close to done

An agent already *is* a configuration — `AgentConfig` is 30 fields of typed
data, and `AgentToolbox` already derives what an agent may reach from its
grants alone. What is missing is the thing that makes a configuration product
trustworthy: **something that reads the configuration and tells you it is
broken.**

The old compiler turned `nodes + edges → StateGraph`. The new one turns
`blocks → AgentSpec`, and its diagnostics are what the errors window renders.
Same shape of problem, different input:

```
DAG era     nodes + edges  ──compiler──▶  StateGraph   ──engine──▶  run
Block era   blocks         ──compiler──▶  AgentSpec    ──runtime──▶ run
                                │
                                └──▶ diagnostics ──▶ the errors window
```

### The problem this actually solves

Five knobs in the current config are stored, validated, round-tripped — and
never read by anything:

| Knob | State |
|---|---|
| `useOrgContext` | Dead. Nothing reads it |
| `compaction` | Dead. Nothing reads it |
| `recursiveContext` | Dead. Nothing reads it |
| `indexing` | Dead as an agent knob |
| `trigger` (mode + cron) | **Stored, validated, never fires.** No scheduler exists |
| `screen_context` | Sent on every chat message by `ChatPanel`. No backend code reads it |

Plus `shell` and `fileOps`, which the runtime refuses outright.

A configuration UI whose controls do nothing is worse than no UI. Every block
below must be **compiled** — either it changes behaviour, or the compiler
reports it as unsupported and the UI shows it. Nothing is allowed to sit in
between, which is the state all five are in today.

---

## 2. The block model

Six block kinds, each a typed slice of the existing config. This is a
*regrouping*, not a new data model — `Workflow`'s agent columns already hold
all of it, which is why this is affordable.

| Block | Holds | Column today |
|---|---|---|
| **Trigger** | how it starts: manual / schedule / webhook | `trigger` |
| **Model** | provider, model, temperature | `llm_provider`, `llm_model` |
| **Capability** | one tool, connector, KB or skill — *one block each* | `tool_grants`, `agent_context` |
| **Guardrail** | autonomy, spend cap, egress, review | `guardrails` |
| **Context** | compaction, environment (§7) | `agent_context` |
| **Brief** | name, instructions | `name`, `description` |

Capabilities are **one block per item** on purpose: that is what makes the
builder composable and lets each carry its own diagnostic ("Gmail — no
credential connected") instead of one lump labelled "connectors".

---

## 3. The compiler

New: `agents/agent_compiler.py`.

```python
compile_agent(agent, user) -> AgentSpec | Diagnostics
```

It answers one question the platform cannot answer today: **will this agent
work?** Checks, in order of how badly they break a run:

**Blocking — the agent cannot run:**
- No model, or a model whose provider has no verified credential
  (`llm.preflight` already knows how to decide this — reuse, do not re-derive)
- A schedule trigger with an invalid or absent cron
- Brief is empty — nothing to instruct the model with

**Degraded — it runs, but not as configured:**
- A granted capability the runtime refuses (`shell`, `fileOps` —
  `UNSERVED_GRANTS`)
- A connector granted with no credential connected
- A knowledge base or skill id that no longer exists
- `mcp` granted while the user has no enabled MCP server
- Spend cap already reached this month

**Advisory:**
- No capabilities granted at all — it can only talk
- Autonomy `full` combined with a credentialed connector

Every diagnostic carries `{level, block_id, message, fix}`. `block_id` is what
lets the UI highlight the offending block instead of printing a list — the same
reason the run trace carries `node_id`.

**The compiler is also the runtime's gate.** `run_agent` calls it and refuses
on a blocking diagnostic, so the errors window and the actual refusal come from
one source. Two code paths deciding "is this agent OK" is exactly how the
permissions screen drifted from the runtime before.

---

## 4. Triggers that actually fire

Today `trigger` is decoration. Three modes ship:

- **Manual** — the Run button. Works today.
- **Schedule** — cron. Swept by a periodic task, reachable **both** as a Celery
  beat task and a `manage.py` command. That dual reachability is not optional:
  local dev runs without Redis, and a beat-only design silently never fires —
  the mistake `notifications/reminders.py` already documents.
- **Webhook** — a per-agent URL. `api/webhooks/<user_id>/<path>` already
  exists for workflows; agents reuse the receiver rather than growing a second.

Out of scope for the ship: event triggers, polling triggers, trigger chaining.

---

## 5. Templates become agent types

`WorkflowTemplate` stores `nodes` / `edges` — the wrong shape. Templates become
**pre-filled block configurations**, which is what makes "different types of
agent" a real product concept rather than a blank form:

Researcher · Support responder · Data extractor · Scheduled monitor ·
Document Q&A

Each ships as a fixture: brief, model, capability blocks, guardrails, trigger.
Installing one clones the config into the user's account — no DAG involved.

Migration: add `kind` to the template model and a `config` JSON column; leave
existing DAG templates readable but stop offering them for new agents.

---

## 6. Scope cuts — what ships by not being built

Named explicitly, because "reduce scope" only works if the cuts are decided
rather than discovered:

| Cut | Reason |
|---|---|
| **Workflow editor page** | Already unlinked. Delete the route, `WorkflowEditor.tsx`, `WorkflowsDashboard.tsx`, `AutomationTabs.tsx` (last two are already orphaned) |
| **`buddy/` + `browserOS/`** | **Deleted, both.** One subsystem, not two — see §6.1 |
| **Evals + Tuning** | ~1,792 lines that record a queued row nothing picks up. Remove from `INSTALLED_APPS` and delete, or finish — not a third option |
| **Multi-agent / orchestrator tier** | `AGENT_WORKFLOW_UNIFICATION.md` stays design-only |
| **Event & polling triggers** | Manual, schedule, webhook only |
| **`compiler/compiler.py` + `executor/engine.py`** | **Not cut.** Leave dormant — deleting them means touching `nodes/`, which is the model layer. Revisit after shipping |

### 6.1 Buddy and BrowserOS — **DONE 2026-08-16**

They read as two apps and are one. `buddy/views.py` imports `browserOS.models`
directly; its whole action layer — `_open_app`, `_close_app`, `_set_wallpaper`,
`_create_notification` — manipulates `OSAppWindow` and `OSWorkspace`. Buddy *is*
BrowserOS's command processor. Deleting one and keeping the other leaves a
broken import, so the cut is both or neither.

This also corrects the earlier "disconnect BrowserOS" cut, which would not have
disconnected it. `api/browseros/` is not what the BrowserOS frontend calls —
its only backend calls are `POST /api/buddy/commands/`, from `ChatbotApp.tsx`
and `BuddyPanel.tsx`. Unregistering the `browserOS` router alone would have left
the live channel open.

**What is removed**

| | |
|---|---|
| `Backend/buddy/` | 723 lines, no models |
| `Backend/browserOS/` | 221 lines, 3 tables (`OSWorkspace`, `OSAppWindow`, `OSNotification`) |
| `BrowserOS/` | **Kept on disk.** No git history to recover it from, and nothing builds or serves it. It now runs with no server behind it |
| `useBuddy.ts` + `ChatPanel` buddy UI | 147 lines plus the toggle, status dot and action banner |
| `ws/buddy/` route, `BuddyConsumer`, `prompts._buddy_block` | |

**The one real loss.** Chat had screen awareness: `useBuddy` pushes a
DOM capture over `ws/buddy/` on connect and on navigation, warming the cache
`prompts._buddy_block` read. That was genuine, small, and is gone. (The *other*
path — `screen_context` in the chat POST body — loses nothing, because no
backend code has ever read it.) The `BuddyConsumer` docstring calls the socket
"a cache-warming convenience, not the path context normally takes"; it is in
fact the only path. Worth knowing which sentence was stale before deleting.

**Nothing else depends on either app.** Outside `buddy/` itself, no module
imports `browserOS`. The cut is clean.

**Outcome.** Backend and `better-n8n-frontend` are clean of both: apps deleted,
`INSTALLED_APPS` / `urls.py` / `streaming/routing.py` unwired, `_buddy_block`
and its two `chat/tests/test_context.py` assertions removed (the surrounding
system-prompt assertions survive intact), `useBuddy.ts` deleted, and the
`ChatPanel` toggle / status dot / action banner and the `screen_context` request
field removed with it. `BUDDY_CONTEXT_CACHE_TTL` was documented in `CLAUDE.md`
and defined nowhere — also gone.

Dropping `browserOS` from `INSTALLED_APPS` orphans its three tables rather than
dropping them. Left as-is: harmless, and a removal migration would have to live
in an app that still exists.

Verified: `manage.py check` clean, **908 backend tests pass, 17 skipped, 0
failed**; frontend `tsc -b --force` clean; the 5 red frontend tests are the
pre-existing deliberate ones and are untouched by this.

### 6.2 The performance audit, intersected with the cuts

A performance pass (2026-08-16) produced ~40 findings across the three trees.
Most of the value is in reading it *against* §6 rather than working it top to
bottom: a third of it is on code this plan deletes, where the fix is the
deletion.

**Zero work — the surface is going.**

| Finding | Why it evaporates |
|---|---|
| Autosave stringifies the whole graph per drag frame (`WorkflowEditor.tsx:642`) | File deleted |
| Templates search fires per keystroke (`Templates.tsx:28`) | Templates rewritten as block configs (§5) |
| `pages/Orchestrator.tsx`, `useWebSocket` | Already unreachable |
| Evals / Tuning | Finish-or-delete, §6 |
| **All three BrowserOS re-render findings**, plus its dead networking stack | Disconnected, §6.1 |
| Activation deep-compares every historical run (`agents/views/workflows.py`, `workflow_detail`) | Reachable **only** from `WorkflowEditor` → `useWorkflowDeployment`. Drops to API-only once the page goes |

**Dormant — real, but in code §6 keeps asleep.** King's O(n²) thought loop
(`executor/king.py`, `_generate_thought` and `after_node`) and the duplicate
zombie-reap loops. Not worth
touching while the DAG executor is parked.

**Ships — do these with Stage 2**, because they are on surfaces that survive:

- **Chat re-renders at 10 Hz.** Two 100 ms thinking timers
  (`StandaloneChat.tsx:196`, `ChatPanel.tsx:111`) re-render the tree, and
  `MarkdownMessage` has no `memo` so every message re-parses each tick. Isolate
  the timer into one component; memoize the renderer. Chat is the landing
  surface — this is the most visible item on the whole list.
- **Three identical `/nodes/models/` requests per chat load.** `useAIModels`
  has no cache and three components mount it, and `nodeService.ts:60` adds a
  `?t=${Date.now()}` cache-buster that defeats the HTTP layer too.
- **Cheap backend wins:** `logs/logger.py:230` sums `tokens_used` by loading
  every node log with its `output_data` JSON → `aggregate(Sum(...))`;
  `logger.py:456` loads a whole `ExecutionLog` row to read two FKs → `.only()`.
- **Per-chunk embedding on ingestion** (`inference/engine.py:472`) — a batch
  `_embed_texts` already exists and is not called.
- **RAG query embedded twice per search** (`inference/views.py:384`).
- Unmemoized context values (`AuthContext`, `AssistantContext`, `ThemeContext`).

**Two systemic findings the item-by-item list does not surface:**

1. **Pagination is configured and universally bypassed.** `settings/base.py:268`
   sets `PageNumberPagination` at `PAGE_SIZE: 20` — which never applies to
   `@api_view` function views, and there are ~60 (25 in `agents/views/`
   alone). The "unbounded list" findings for KB documents, execution detail and
   the WebSocket connect payload are three symptoms of one gap, not three bugs.
   This is the only item here that is a reliability risk rather than a
   slowness: an unbounded execution-detail response on a long run is how the
   1.9 GB box dies.
2. **No code splitting at all.** No `React.lazy` anywhere in
   `better-n8n-frontend`, so `reactflow`, `dagre`, `react-markdown` and
   `remark-gfm` all sit in the initial bundle — behind a *chat* landing page.
   Deleting the workflow editor removes the main reason ReactFlow loads eagerly,
   which makes Stage 2 the natural moment to split.

**Cold start is unmeasured.** Connector registration
(`nodes/handlers/registry.py:210`) and embedder resolution
(`inference/engine.py:125`) both run at *import* time — paid by every
`manage.py` call, Celery worker boot and test session. The audit is entirely
request-scoped and does not look before the first request.

**Two introduced by the agent/workflow merge, worth fixing while nearby:**
`agent_runtime.py:555` finds a paused run by `input_data__thread_id`, an
unindexed JSON lookup that full-scans `ExecutionLog` on every HITL approval; and
`agent_stream.py:171` writes one `NodeExecutionLog` INSERT per tool call through
`sync_to_async`, a thread hop each. Neither matters at current volume; both get
harder to unpick later.

**Dead code (~25 files).** Delete with Stage 2 — with one correction to the
audit: **`nodeRegistry` is not dead.** `GenericNode.tsx` imports it and
`TemplateDetail.tsx` imports `GenericNode`, routed at `/templates/:id`. The
audit traced it through `WorkflowEditor` and missed the second parent.
`useGraphStore` *is* dead (only its own test imports it) — and `CLAUDE.md` still
describes it as the single source of truth for canvas state. Fix that line in
the same change; a stale instruction file misleads every future session.

---

## 7. Stages

Each leaves the tree green and is independently useful.

**Stage 1 — Compiler + diagnostics (backend).**
`blocks.py`, `agent_compiler.py`, `GET /agents/{id}/diagnostics/`, and
`run_agent` refusing on blocking diagnostics. Delete `recursiveContext`,
`indexing`, `useOrgContext` in the same change — a knob that does nothing has
no place in a product whose pitch is "configuration you can trust". Migration
drops them from stored `agent_context`.

**Stage 2 — Scope cuts (§6) + the surviving perf work (§6.2) — DONE 2026-08-16.** Delete the
workflow editor surface, the ~25 dead files, and Evals/Tuning; `buddy/` +
`browserOS/` are already done (§6.1). Then the performance items that are *not*
on deleted surfaces — the chat 10 Hz re-render, `useAIModels` dedupe, the
`logger.py` aggregate and `.only()`, batch embedding, and pagination on
function-based list views. Deletion and optimisation belong in one pass because
the deletion decides which optimisations are worth doing at all.

*What landed:* the workflow editor and its 59 orphaned modules deleted (a
reachability walk from `main.tsx`/`App.tsx` decided the list, not a grep — the
`nodeRegistry` entry in §6.2 was a false positive of mine and it *is* dead);
`evals/` + `tuning/` removed from both trees along with `DatasetSerializer.used_by`,
which could only ever list their rows; the chat clock isolated into
`ThinkingTimer` and `MarkdownMessage` memoised; `useAIModels` moved to React
Query and its `?t=` cache-buster dropped; `logger.py` summing tokens DB-side and
writing thoughts from three ids rather than a whole row; ingestion embedding
batched; the RAG query embedded once for all tiers; caps on execution detail,
the socket catch-up frame and the uncursored document list; the three context
values memoised; route-level code splitting.

*Kept deliberately:* `CanvasAgentBar`, `CanvasAgentContext`, `useCanvasAgent`
are unreachable but retained — the sidebar is wanted later.

*One seam this opened:* installing a DAG template still creates a workflow, but
there is no longer a page that opens one. `TemplateDetail` now lands on
`/agents` and the created workflow is only reachable over the API. Stage 4
resolves it by making templates agent configs; until then this is a known
dead-end, not an oversight.

**Stage 3 — Triggers.** Schedule sweep (beat + management command) and webhook
receipt. Compiler already validates the cron from Stage 1.

**Stage 4 — Templates as agent types.** Model change, fixtures, install flow.

**Stage 5 — The configuration UI.** Block-based builder replacing
`AgentBuilder`'s flat form: capability blocks, a trigger section, and a
**diagnostics panel docked at the bottom** — same placement and behaviour as
`AgentRunLog`, which already proved the pattern. Blocks with diagnostics carry a
severity stripe; the panel says what is wrong and how to fix it, and the Run
button is disabled with the reason on it when a blocking diagnostic exists.

**Stage 6 — Compaction.** Deliberately last, because it is the only stage that
changes what the model sees at runtime and it wants the diagnostics work
finished first. When a run's transcript approaches the model's window,
summarise the oldest completed tool exchanges into a single synthetic message
and keep the rest verbatim. Constraints:

- Never compact the system prompt, the user's goal, or the most recent turn.
- Compaction is **recorded** — the run trace shows it happened, because a user
  reading a trace with a hole in it should be told there is a hole.
- Bounded and idempotent: compacting twice must not summarise a summary.
- The `compaction` knob finally means something; if left off, a run that would
  overflow fails with a clear diagnostic rather than silently truncating.

---

## 8. Verification

Backend `pytest`:
- Every diagnostic level, and the `block_id` pointing at the right block
- `run_agent` refuses on a blocking diagnostic and the message matches what the
  panel shows — one source, asserted from both sides
- A scheduled agent fires from the management command with no Redis
- Deleted knobs: no reference survives in code, serializer, or stored JSON
- Buddy/BrowserOS removal: `ws/buddy/` and `api/buddy/` both 404, the system
  prompt still carries everything §7's context tests pin, and the app boots with
  neither app installed
- Compaction: preserves system prompt + goal + latest turn; is idempotent; is
  recorded in the trace

Frontend `vitest` + `tsc -b --force` + `npm run lint` + `npm run build`:
- Diagnostics render at the right severity and disable Run when blocking
- Removing the workflow editor leaves no dangling import, and `TemplateDetail`
  still renders (it is the surviving `nodeRegistry` consumer — §6.2)
- No list endpoint returns an unbounded collection: assert a cap on execution
  detail, KB documents, and the WebSocket connect payload

End to end: install a template → the builder shows its blocks → remove the
model credential → the panel says so and Run is disabled → reconnect → schedule
it → it fires → the run trace shows a compaction event on a long run.

---

## 9. What this deliberately does not answer

- Whether `compiler/` gets renamed once it holds no compiler (§6 keeps it).
- Whether workflows come back as an agent-callable "hands" tier
  (`AGENT_TEMPLATES.md` §7). Nothing here forecloses it; nothing here builds it.
- Whether BrowserOS returns. Deleting it forecloses nothing conceptually, but
  this tree has no git history — archive the directory rather than `rm` it if
  there is any chance of wanting it back.
