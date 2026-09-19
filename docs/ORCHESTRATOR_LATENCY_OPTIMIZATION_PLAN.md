# Orchestrator Response Time Optimization Plan

## 1. Executive Summary

This plan addresses slow response times in the orchestrator and agent run execution pipeline (`agents/agent/orchestrator.py`, `agents/agent/runtime.py`, `chat/turn/agent.py`). 

> **Read §3d first.** As of 2026-09-18 this document has real production
> measurements, and they disagree with the profiling below on two of its four
> points: tool discovery is 6–35ms (not seconds), and the dominant cost is
> provider TTFT, which none of the four items names. The four bottlenecks are
> kept as written because the phases refer back to them.

Profiling reveals four major architectural bottlenecks contributing to high latency (measured 2026-09-18 at 30s–83s per turn):
1. **SQLite Write-Lock Contention:** High-frequency persistence of `AgentTurn`, `AgentStep`, and checkpoint commits stalls ASGI threads and async I/O.
2. **MCP Discovery Latency & Cold Connectors:** Stdio-based MCP tools incur cold start costs (`npx` execution taking 8s–20s) if tool cache entries lapse.
3. **Sequential Tool Execution & Companion Media Overhead:** When an agent issues queries or file/search operations, default executions and auto-triggered media searches run sequentially.
4. **LLM Provider Round-Trips & Thinking Overhead:** Deep reasoning/high effort configurations on intermediate tool-dispatching steps inflate Time-to-First-Token (TTFT).

---

## 2. Target Performance Benchmarks

| Metric | Current State | Target State | Optimization Applied |
|---|---|---|---|
| **Turn 1 Tool Discovery** | 2,000ms – 18,000ms (cold npx) | **< 10ms (cached)** / **< 150ms** | Redis Cache Layer + Background Warmup |
| **AgentTurn / Step DB Commit** | 80ms – 600ms (SQLite lock wait) | **< 5ms** | PostgreSQL Pool (`psycopg3`) |
| **Tool Dispatch Batch** | 3,000ms – 8,000ms (Serial) | **500ms – 1,500ms (Parallel)** | Safe concurrent execution of read-only MCP/internal tools |
| **Model TTFT on Routine Turns** | 3,500ms – 8,000ms (thinking models) | **< 1,200ms** | Dynamic effort scaling (`effort: "low"` / `"none"` on intermediate steps) |
| **Companion Media Search** | +1,500ms synchronous | **max(search, images), not the sum** | Strip pre-fetched in Pass 3 alongside the search; collected into the same `meta` write (see `tools_node`, `_fetch_companion_images`). Deliberately *not* fire-and-forget: `meta` persists at turn close, so a detached task writes into a dict nobody saves and emits down a closed sink. |

---

## 3. Detailed Work Breakdown & Phases

### Phase 1: Database Migration & Write-Lock Elimination (Days 1–2)
* **Status: half shipped — verified against the running container 2026-09-18.**
  The **application database is PostgreSQL**: `aiaas-db` is `postgres:16-alpine`
  and the backend holds `DB_ENGINE=postgres` /
  `DATABASE_URL=postgresql://admin:***@db:5432/app_db`. The `AgentTurn`,
  `AgentStep` and `ExecutionLog` writes this phase was written about no longer
  contend on a single writer lock.
* **The previous entry here was wrong, and the way it was wrong is the lesson.**
  It read "NOT shipped", sourced to `docker-compose.ec2.yml` — a *retired*
  predecessor that still describes SQLite. `docker-compose.prod.yml` is what
  production runs. A compose file in the tree is not evidence about a deployed
  box; the container's own env is. Check the runtime, not the repo.
* **What is genuinely still open is the checkpointer.** `AGENT_CHECKPOINTER` is
  unset in production, so it falls to the `'sqlite'` default in
  `settings/base.py:349`, and `/app/data/checkpoints.sqlite3` is live and being
  written (1.5 MB WAL, mtime within the hour, observed 2026-09-18). Every
  LangGraph super-step of every chat turn and agent run still goes through one
  SQLite writer on the shared volume — contention that surfaces as a stall,
  never as an error and never in a log, exactly as this phase said. It is on
  its own file rather than `db.sqlite3`, which is the deliberate part; that it
  is SQLite at all is the remaining half.
* **Action (remaining):** set `AGENT_CHECKPOINTER=postgres` with
  `AGENT_CHECKPOINT_DSN`. Note `checkpoints.py:137` raises at startup on a
  misconfigured DSN rather than degrading to memory, so this is a deploy that
  fails loudly or works — verify the DSN before the restart, not after.
* **Problem:** Every model call and tool execution writes to `AgentTurn`, `AgentStep`, and `ExecutionLog` through `sync_to_async`. Under SQLite, database-level locking stalls the ASGI event loop.
* **Actions:**
  1. Migrate production to PostgreSQL using `psycopg[binary,pool]` (as detailed in `postgres_production_migration_plan.md`).
  2. Implement a dedicated connection pool (`min_size=5, max_size=20`) to serve async agent streams without queueing.
  3. Ensure LangGraph checkpoints write to PostgreSQL rather than local SQLite files (`checkpoints.sqlite3`).

### Phase 2: Production Redis Cache & Pre-Warmed MCP Tool Registry (Days 2–3)
* **Status: shipped.** `SOFT_TTL_SECONDS = 1800` (30 min), `HARD_TTL_SECONDS = 86400`
  (24h) in `mcp_integration/tool_cache.py` — freshness is enforced by
  invalidation on edit, not by ageing out. Redis `CACHES` (Django's built-in
  `RedisCache`, separate logical DB from channels/Celery) is configured in
  `workflow_backend/settings/base.py` and on wherever `USE_REDIS_CHANNEL_LAYER`
  is on, which includes deployment. `warm_cache` pre-lists on connector
  save (`mcp_integration/views.py`); a lapsed entry is served stale while
  `_refresh_in_background` re-lists behind the turn, so resumed conversations
  never pay a cold `npx` in front of their first token. Per-turn memoisation
  (`mcp_memo` / `AgentToolbox._mcp_descriptors`) keeps later iterations off
  the cache entirely. Agent runs additionally narrow listings to their
  `connectors` scope; chat passes the whole workspace by design.
* **Actions (all done):**
  1. ~~Configure Redis in Django `CACHES`~~ — done in `settings/base.py`.
  2. ~~Increase `SOFT_TTL_SECONDS` to 1800, `HARD_TTL_SECONDS` to 86400~~ — done.
  3. ~~Pre-warm tool descriptors~~ — done on save; process-start warming was
     considered and rejected (tool lists are per-user credentialed reads, so
     there is no safe global set to warm at boot, and enumerating every
     user's connectors would stampede `npx`).

### Phase 3: Parallel Execution for Read-Only MCP & Companion Calls (Days 3–4)
* **Problem:** 
  - In [`chat/turn/agent.py:1189`](file:///C:/Users/91700/Desktop/AIAAS/Backend/chat/turn/agent.py#L1189), only built-in tools in `tool_registry.PARALLEL_TOOLS` can execute in parallel. All MCP tools default to serial dispatching.
  - Companion media searches (`image_search`, `video_search` on web topics) run during the synchronous turn flow.
* **Status: shipped, with one correction.** Parallel dispatch is live: built-ins
  declare `parallel=True` on `@tool()`, MCP calls overlap on a name guess
  (`permissions.mcp_reads_only`) — the same guess approval gating already
  trusts — and sensitive calls always stay serial (`tools_node` passes 3–4).
  The companion web-search image strip is pre-fetched in Pass 3 alongside the
  search and collected in Pass 4 (`_fetch_companion_images`), turning
  search+images into max() instead of a sum.
* **Actions:**
  1. ~~Concurrency tagging for read-only MCP descriptors~~ — done via
     `mcp_reads_only` (a stored tag is impossible: MCP names are minted at
     runtime by third parties).
  2. ~~Parallelize safe reads~~ — done.
  3. ~~Convert companion searches to fire-and-forget background tasks~~ —
     **rejected in favour of Pass-3 overlap.** Detaching loses the panel:
     `meta` is read-modify-write persisted when the turn closes and the sink
     belongs to the turn, so a still-running task writes images nobody saves
     down a sink whose run ended. Overlap is the free half of the win;
     detaching trades a visible panel for a lost one.

### Phase 4: Model Effort & Token Generation Tuning (Days 4–5)
* **Problem:** When an agent is set to deep reasoning, each micro-turn generates extensive "thinking" tokens before calling a tool, multiplying total latency across 4–6 iterations.
* **Status: shipped.** `iteration_effort` runs middle loop iterations one ladder
  rung below the chosen level; first and last stay at the user's choice
  (dropping two rungs was considered and left alone — a `high→low` middle
  without log evidence trades answer quality for unmeasured TTFT). Shipped
  default effort is `medium` on both sides (frontend `DEFAULT_EFFORT`,
  `UserProfile.llm_effort`). Ancillary calls (curation fold, follow-up
  questions) already pass `effort="none"`. Follow-up suggestions are now also
  skipped outright when the main turn exceeded `FOLLOW_UPS_SLOW_TURN_SECONDS`
  (60s default): they are awaited before the message row is written, so on a
  slow turn they delay persistence for questions nobody asked for.
* **Actions:**
  1. ~~Dynamic effort tuning~~ — done (one rung, pinned in tests).
  2. ~~Fast sub-model for curation~~ — done: the run's `summaryModel`, else the
     platform-pinned `CONTEXT_SUMMARY_MODEL`, always at `effort="none"`.

### Phase 5: Admission Queue & Concurrency Scaling (Day 5)
* **Problem:** In [`workflow_backend/thresholds.py`](file:///C:/Users/91700/Desktop/AIAAS/Backend/workflow_backend/thresholds.py#L340), `MAX_CONCURRENT_RUNS_PER_USER = 3` and `MAX_CONCURRENT_RUNS_TOTAL = 12`. Simultaneous runs queue up to `ADMISSION_WAIT_SECONDS` (120s).
* **Status: half shipped.** Admission limits are env-configurable
  (`RUNS_PER_USER_LIMIT`, `RUNS_TOTAL_LIMIT`, `ADMISSION_WAIT_SECONDS` in
  `workflow_backend/thresholds.py`).
* **Actions:**
  1. ~~Env-configurable concurrency limits~~ — done.
  2. Expose admission wait telemetry in the `/agents/runs/{id}/stream/` SSE/WS payload so clients clearly know if a run is queued vs executing. *(still open)*

---

## 3b. Found while verifying the plan — not one of its phases

**`ExecutionLog` was addressed by an unindexed JSON path.** The plan's profiling
does not mention it, and it was the most expensive query on the orchestrator's
*interactive* path: `input_data__thread_id=` on three call sites — resuming a
paused run (`agents/agent/runtime.py::_find_paused_log`), closing a HITL request
on approve or reject (`agents/agent/hitl.py`), and resolving a delegated run's
parent step (`chat/tools/agents.py`). On SQLite that is a full table scan with a
JSON parse per row, growing with every run the account has ever made, and it is
paid on the two paths a person is actively waiting on: clicking approve, and
delegating.

`ExecutionLog.thread_id` is now an indexed column and all three sites filter on
it. `input_data['thread_id']` is still written — it is what historical rows
carry and what a run's own record shows; the column is the *addressable* copy,
and `logs/models.py` is the only place that has to know they are the same
string.

Migration `logs/0018` backfills it, **paused rows first**. That ordering is the
point rather than a nicety: a paused run whose thread did not come across looks
like a run with no thread, so `_find_paused_log` answers None and the approval
the user just clicked resumes nothing at all — silently, with the checkpoint
still sitting there and nothing raising.

Tests: `logs/tests/test_migrations.py::ThreadIdBackfillMigrationTests`,
`logs/tests/test_checkpoints.py::ThreadIdIsAddressableTests`. The latter asserts
on the **WHERE clause** specifically, because the SELECT list names every column
including `input_data` — a naive `assertNotIn('input_data', sql)` fails against a
query that is entirely correct.

### A caution about the benchmark table in §2

The "Current State" column is unsourced and at least one row does not match the
tree it was written against: `tools_ms` in the existing `[Latency]` line is
typically single-digit milliseconds, not the 3,000ms+ the table implies, because
the descriptor build is cached and now also memoised per turn. Treat the targets
as directional. The `[Latency]` logger is a real instrument; the numbers printed
beside it in §2 are not measurements. **§3d now carries measurements** — the
first ones taken off production rather than off a developer's box.

### A second doc-vs-code drift, found the same way

`tool_provider.py` and `CLAUDE.md` both describe the per-connector agent budget
as 8s. The code says 5s — `AGENT_LIST_TOOLS_TIMEOUT = _float_env("MCP_AGENT_LIST_TIMEOUT", 5.0)`
in `mcp_integration/client.py`, with the compose override left commented out. Tune
against 5s, not 8s.

### The instrument does not cover the segment that matters

`[Latency]` starts inside `agent_node`. Everything `run_chat_turn` does *before*
the graph — preflight, the user-message write, history, user memory, attachment
partitioning, the vision witness, the `/Chat/` folder write, recall, and intent
seeding — is invisible to it. That is 15–20 sequential awaits, and on the
evidence it is where most of the pre-token wall clock lives. **Extending the
instrument backwards over that segment is a prerequisite for trusting any number
in this document**, and is cheap enough to do first.

---

## 3c. Phases 6–9 — added 2026-09-12

Phases 2–4 optimised the **warm** path and left the cold one as the worst case in
both directions at once: a cold connector costs the turn 5s *and* returns no
tools, so the agent is slower and less capable on the same turn, and cannot tell
the user why. These four phases are about the cold path, the loop count, and the
dispatch boundary.

### Phase 6: Give the tool catalogue a floor, and bound the session pool

* **Status: shipped 2026-09-13.** Both halves. `MCPToolCatalogue`
  (`mcp_integration/models.py`, migration `0018`) is the durable tier, wired
  entirely inside `tool_cache.py` — `MCPToolCache` was already the only reader
  and the only writer, so `client.py` did not change. `_pool` is now an
  `OrderedDict` with `_touch` / `_trim_pool` and `MAX_POOLED_SESSIONS`
  (`MCP_MAX_POOLED_SESSIONS`, default 6). Tests:
  `mcp_integration/tests/test_tool_catalogue.py` (14),
  `mcp_integration/tests/test_pool_lru.py` (10).
* **The pool tests deliberately come in two layers.** `PoolLRUTests` drives
  `_trim_pool` directly and proves the *policy*; `PoolBoundIsWiredIntoAcquisitionTests`
  goes through `MCPClientManager._session` with only the transport faked and
  proves the policy is actually *reached*. The first layer alone is the failure
  mode this codebase has hit before — every hop covered, the chain connected
  nowhere — which is what `chat/tests/test_turn_output_e2e.py` exists for on the
  turn side.
* **Left alone, and worth knowing:** `_creation_locks` and `_failures` are still
  unbounded dicts. Both hold tiny values (a `Lock`, a float and a string) and
  pruning `_creation_locks` races with coroutines waiting on one, so the cost of
  a subtle double-create is worse than the bytes. Only `_pool` held a
  subprocess, and only `_pool` is capped.
* **Two decisions made during the build, both narrowing the design.**
  Invalidation drops the stored row as well as the cache key: the tier exists to
  survive *cache loss*, never an *edit*, so a user who has just changed a
  connection still falls through to a live listing exactly as before. And an
  **empty listing is never stored** — every failure path in
  `get_openai_tool_descriptors` returns `[]`, so persisting one would write "this
  connector has no tools" into the durable tier on the strength of a timeout and
  keep answering that way.
* **The per-user question is still open** and the schema does not assume an
  answer: rows are keyed `(server, user)` to mirror the Redis key exactly, with a
  partial unique constraint already reserving one null-user row per server should
  a shared baseline turn out to be correct.

**Problem A — the cache has nothing under it.** `MCPServer` has no tools column:
grep it. The entire tool catalogue exists only in Redis, so the fallback chain is
**Redis → nothing**. A Redis restart, an eviction, a fresh deploy, the 24h hard
TTL lapsing, or a user's first ever turn all land on the same cliff: 5s of dead
air and an empty toolbox.

A tool list is neither secret nor volatile — it changes when someone edits a
connection, and that path already invalidates the key outright. There is no
reason for it to live only somewhere that evicts.

* **Action:** persist the last good listing (tools + `listed_at`) durably, making
  the chain **Redis → database → live handshake**. A Redis miss then costs one
  indexed read (~5ms) *with a full toolbox*, instead of 5s with none. Only a
  genuinely never-listed connection reaches `npx`.
* **Open question, to settle before choosing the table shape:** does `list_tools`
  actually return a different catalogue per user, or the same one? The cache is
  keyed `(server_id, user_id)`, which assumes it differs; the expectation is that
  credentials decide whether a *call* succeeds, not which *tools exist*. If the
  catalogue is server-level, curated rows share one baseline and a user with no
  entry of their own still gets a correct toolbox instantly. **Unverified — this
  is a short experiment and it decides the schema.**
* **Why staleness is acceptable here:** this is the same trade `HARD_TTL_SECONDS`
  already makes and states — a stale list costs one failed tool call the model
  sees and can react to, a miss costs seconds of silence. Persisting it is that
  decision finished, not a new one.

**Problem B — `_pool` is unbounded.** `_pool: dict[_PoolKey, _PoolEntry]` has a
TTL (`SESSION_TTL = 300s` idle) and **no size cap**. Each live stdio entry is a
Node subprocess. On a ~900 MB box (`free -m` reports 913 MB total; the backend
container is capped at 384 MB), N users × M connectors grows until the TTL
happens to catch up, which is a memory ceiling set by user behaviour rather than
by us. `_creation_locks` and `_failures` are likewise never cleaned.

* **Action:** make the pool an **LRU with a hard size cap**, evicting the
  least-recently-used session when full. This buys two unrelated things with one
  structure, which is why it is worth doing properly:
  1. **Memory safety.** The subprocess count is bounded by configuration, not by
     how many connectors happen to be popular this minute. This is the half that
     protects the box.
  2. **Warmth for hot connectors.** Today a connector dies at 5 minutes idle
     regardless of how heavily it is used, and pays a full reconnect afterwards.
     Under an LRU, recency decides: a connector used every turn never falls out,
     while a rarely-touched one is the first to go. Warmth stops being a fixed
     timer and starts tracking actual use.
* **Keep the TTL alongside the cap.** They answer different questions — the cap
  is "how many may live", the TTL is "how long may a dead-idle one hold a
  subprocess". An LRU alone would keep the N most recent sessions alive for ever
  on a quiet box, holding subprocesses nobody is going to use.
* **Eviction must stay in the owning task.** `_SessionWorker` exists precisely
  because an anyio task group may only be exited by the task that entered it, and
  the old cross-task close orphaned one subprocess per eviction. An LRU evicts
  *more often* than a TTL does, so this constraint takes more load, not less —
  route every LRU eviction through the existing `_evict`, never a new path.

### Phase 7: On-demand connector loading, warmed speculatively

**Problem:** every turn pays to list every connector, and a typical turn uses
none or one. Discovery is on the critical path for work that mostly does not
happen.

The split falls in the right place: the **connection list** is cheap (a few
indexed rows, no subprocess); the **tool list** is what costs a handshake.

* **Action:** put the *connections* in front of the model and defer the *tools*.
  The agent sees that Gmail, Slack and Drive exist and calls `load_tools(<name>)`
  when it decides it needs one — so the handshake happens for one server, at the
  moment of need, instead of for all of them on every turn.
* **The list goes in `build_context_update`, never the system prompt.** It
  changes whenever a connection is added or switched off; in the baseline it is
  the clock trap exactly, and the forfeited prefix cache would cost more than the
  deferral saves. `CORE_RULES` rule 12 stays where it is — it is static text
  *about* connectors and remains correct.
* **Be honest about the trade.** A turn that *does* use a connector gets an extra
  model round trip on top of the handshake. The common case gets faster; the
  connector case gets worse — which is exactly why Phase 6 lands first, so
  `load_tools` is a ~5ms read rather than a ~21s dial.
* **Speculative warming, which is what makes this net-positive:** the first model
  call no longer needs MCP descriptors, so connector listing can run
  *concurrently with it* rather than in front of it. By the time the model asks
  for Gmail, the entry is usually warm. This is free speculation — it occupies no
  critical path and its failure mode is the status quo. Scope it to the
  connections named in the context block, and let a `load_tools` call in flight
  join the speculative listing rather than starting a second one.
* **Not "cache in a process-global dict".** There are already two memory tiers
  above Redis — the per-turn memo (`mcp_memo`, `AgentToolbox._mcp_descriptors`)
  and the live pool. A third process-local dict dies on every deploy and, once
  the ASGI tier is multi-process (Phase 9), gives each worker its own copy and
  therefore N times the cold starts. The missing tier is **below** Redis, and
  that is Phase 6.

### Phase 8: Batched and speculative tool dispatch

**Problem A — the parallel engine may be idling.** Parallel dispatch shipped in
Phase 3, but it only helps *within one batch*. Nothing in `chat/turn/prompts.py`
tells the model to issue several calls together, and rule 3 (TOOL ECONOMY) pushes
the other way — it discourages tool use and says nothing about batching. A model
that calls one tool, waits, then calls another gets no benefit from
`asyncio.gather` at all.

* **Action:** add a batching rule to `CORE_RULES` — when several independent
  lookups are needed, issue them in one turn. **No new code, no migration, and it
  changes what every other phase here is worth**, because total time is
  `iterations × (think + write + tools)` and batching removes whole iterations
  rather than shaving one. Do this first and re-measure before building anything
  else in this phase.
* **Status: shipped 2026-09-13, in two places — and the second is the one that
  matters.** Extending rule 3 rather than adding rule 14 keeps the numbering
  (`MEMORY_ON_RULE` is 14) and puts the correction where the miscue is. But
  `agents/agent/runtime.py::build_system_prompt` builds a **completely separate**
  prompt and has never included `CORE_RULES`, so a chat-only edit would have
  reached everything except the case it was written for: a chat turn takes a
  handful of iterations, an agent run may take forty. The same guidance is now
  in that prompt's WORKING METHOD block. The claim it makes is already pinned by
  `chat/tests/test_parallel_tools.py::test_read_only_calls_overlap`, which is
  what makes it safe to state as fact to the model.
* This is safe to state plainly to the model because it is already true of the
  runtime: calls in one turn are independent by construction (the model issues
  them before seeing any result), which is the premise `tools_node` is built on.

**Problem B — dispatch waits for the whole response.** `_run_model` accumulates
the entire stream, *then* returns, *then* `tools_node` runs. When a model emits
three calls, none starts until the third has finished streaming.

* **Action:** dispatch a call speculatively as soon as its arguments finish
  streaming, while the rest of the response is still arriving.
* **Bound it hard, by the rules already in the tree.** Speculate only on tools
  that are `effect="read"`, `parallel=True`, not in `SENSITIVE_TOOLS`, and not
  gated by the run's approval policy — an MCP tool whose name was minted at
  runtime never qualifies, which is the same deny-by-default the autonomy ladder
  already uses. A speculative call must be cancellable, and cancelling it must
  leave nothing behind.
* **Approval settling stays where it is.** `interrupt()` discards a node's writes
  and re-runs it from the top; speculating past a gate would dispatch the safe
  half of a batch twice, which is the bug the settle-then-run split exists to
  prevent. Speculation happens *before* `tools_node`, so it must hand its results
  in as already-resolved values and change neither the settle pass nor the
  call-order recording in passes 3–4.
* **Expect a modest win** — a few hundred milliseconds, since tool arguments are
  short. Sequence it **after** Problem A and after Phase 6; it is the most
  intricate item here and the least valuable.
* **Not fire-and-forget.** A tool still running when the turn closes writes into
  a `meta` dict nobody saves, down a sink whose run has ended — the reason Phase
  3 rejected detaching the companion image search. Overlap inside the turn is the
  safe form of this idea.

### Phase 9: The two deployment facts every phase above sits on

Neither is a code change, and both cap what the rest can achieve.

* **SQLite in production** (Phase 1, still open). Lock waits are invisible and
  land on writes every turn makes.
* **Daphne runs a single process** (`Dockerfile` CMD, no worker fan-out). One
  event loop serves every concurrent chat turn and agent run. Note the coupling:
  `agents/admission.py` says so itself — the semaphores are per-process, so the
  moment the ASGI tier becomes multi-process the effective limit silently becomes
  `workers × RUNS_TOTAL_LIMIT`, and the fix is a shared counter in Redis, not
  smaller numbers locally.

### Sequencing

1. ~~**Extend `[Latency]` back over the pre-model segment**~~ — **done
   2026-09-13.** `pipeline._PhaseTimer` emits one `[Latency] pre-model` line per
   turn, non-zero phases only, the same contract `_log_latency` keeps. Tests:
   `chat/tests/test_pipeline.py::PreModelLatencyInstrumentTests`.
2. ~~**Phase 8, Problem A** (the batching rule)~~ — **done 2026-09-13**, chat and
   agent prompts both.
3. ~~**Phase 6** — the database tier, then the LRU pool.~~ — **done 2026-09-13.**
4. **Phase 1** — get the *checkpointer* off SQLite. *(half done: the
   application DB is PostgreSQL in production as of 2026-09-18; LangGraph
   checkpoints are still a SQLite file. Ops, not code.)*
5. **Phase 7** — on-demand loading with speculative warming. *(open)*
6. **Phase 8, Problem B** — speculative dispatch. *(open)*

### What the instrument said the moment it existed

The first run of `PreModelLatencyInstrumentTests` printed:

```text
[Latency] pre-model total=16ms intent=chat vision_witness=16ms
```

On an **empty test database**, with no attachments and no credentials, the
vision-witness resolution was the entire pre-model cost. `resolve_witness` is
three uncached queries (`_configured`, `_has_key`, `_retired`), it runs once in
the pipeline and then again inside `get_available_tools` via
`_requirement_met("vision")` on **every agent iteration** — for an answer that
cannot change during a run. `tools_config` got a Redis-backed overlay cache for
exactly this read shape; this never did.

**Picked up and shipped 2026-09-13** — `WITNESS_CACHE_TTL` (60s), caching the
*absence* of a witness as well as the presence, and resolving live on a cache
failure. Production confirms it: the pre-model line on a real turn now reads
`vision_witness=9ms` inside a `total=38ms` segment (2026-09-18), against the
16ms-of-16ms it was when the instrument first ran.

---

## 3d. First production measurements — 2026-09-18

Pulled from `docker logs aiaas-backend` on the live box. Every prior "current
state" figure in this document was an estimate; these are not. The sample is
small and honest about it: **six model calls in 24 hours**, which is all the
traffic there was.

```text
it=0 tools=35ms(n=59) ttft=3215ms  total=3243ms  prompt=13142 cached=0(0%) out=230  effort=low
it=1 tools=8ms(n=59)  ttft=2082ms  total=5849ms  prompt=13383 cached=0(0%) out=514  effort=minimal
it=0 tools=15ms(n=59) ttft=4190ms  total=9488ms  prompt=11998 cached=0(0%) out=176  effort=low
it=1 tools=6ms(n=59)  ttft=2643ms  total=25398ms prompt=20259 cached=0(0%) out=1000 effort=minimal
it=0 tools=17ms(n=59) ttft=28753ms total=30606ms prompt=12003 cached=0(0%) out=1010 effort=low
it=1 tools=6ms(n=59)  ttft=3706ms  total=47770ms prompt=24061 cached=0(0%) out=1036 effort=minimal
```

All `openrouter/deepseek/deepseek-v4.1-flash`. Wall clock on the worst turn was
83s end to end (`POST /message/stream/` at 06:30:16 → 06:31:39), which tripped
`[Turn] Skipping follow-ups after a 83s turn (over 60s)`.

**What this settles.**

* **`tools_ms` is not a problem and never was.** 6–35ms with 59 descriptors. §2's
  "3,000ms – 8,000ms" was invented, and §4's success criterion of "< 50ms" was
  already met before any phase shipped. The caution in §3b was right.
* **The pre-model segment is not a problem either.** `[Latency] pre-model
  total=38ms intent=chat preflight=5ms sync_choice=2ms user_message=6ms
  model_entry=3ms history=5ms user_memory=2ms vision_witness=9ms file_scope=3ms`
  — 38ms against an 83s turn. The instrument added in the sequencing list has
  now cleared its own segment of suspicion.
* **TTFT is the whole visible hang, and it is provider-side.** Same model, same
  box, same ~12k prompt: 2.0s, 2.6s, 3.2s, 3.7s, 4.2s — and then 28.7s. A 7x
  spread with nothing local moving. **No phase in this document addresses
  this**, because every phase assumes the latency is ours. On this evidence the
  single largest win available is a provider or routing change, not a code
  change, and that belongs in the plan as a phase rather than as a footnote.

**The finding that is actually actionable: `cached=0(0%)`, every call, without
exception.**

Note `it=1` in each pair. That call resends `it=0`'s prefix verbatim and still
reports a zero. Every call reprocesses 12k–24k prompt tokens from scratch.

Two separate things are behind it, and they need separating because only one is
a bug:

1. **The prefix is not stable across iterations.** DeepSeek's caching is
   *implicit* — the provider matches the prefix itself, no breakpoint markers
   required — so a hit should not need anything from us. Getting zero means
   something in the prefix moves between calls. This is a real defect and it is
   the next thing to chase: diff the serialized request between `it=0` and
   `it=1` and find what differs. `build_system_message` is the documented
   session-stable baseline, so whatever moves should not be in it.
2. **We never ask for caching at all.** `cache_control` appears nowhere in the
   codebase — the only matches in `Backend/` are Django's own HTTP header
   helpers under `venv/`. `llm/usage.py:186` *reads*
   `prompt_tokens_details.cached_tokens` off the response and that is the
   entire extent of cache handling. This does not explain today's zero (see
   above), but it means a switch to any provider needing **explicit** cache
   breakpoints — Anthropic through OpenRouter being the obvious one — would
   report `cached=0` by construction, on every call, with nothing in the log to
   say why. The instrument would keep printing the number while nothing was
   ever asked to cache.

**A standing context cost, not a bug:** `prompt=12003` for a one-line question
in a brand-new session. That is system prompt plus 59 tool schemas before the
user has said anything of substance. Chat has no connector scope, so all four
enabled native Google connectors ride every turn. Phase 7 is the entry that
addresses this, and these numbers are its justification.

### What the native-connector move did to Phases 6 and 7

Production runs **no stdio MCP servers at all**. `MCP_ALLOW_STDIO=False`, no
Node in the image, and the connector table reads:

```text
Google Drive | native | enabled      Filesystem | stdio | disabled
Gmail        | native | enabled      Fetch      | stdio | disabled
Calendar     | native | enabled      Memory     | stdio | disabled
Sheets       | native | enabled      Seq.Think. | stdio | disabled
                                     Docs/Notion/Slack | stdio | disabled
```

So the cold-`npx` problem Phase 6 was written against — 21s handshakes, the
subprocess pool, the memory budget, the tool-catalogue floor — **cannot occur in
production**. Those mechanisms are all still correct and still load-bearing in
**local dev**, where stdio is allowed, and they stay. But they should no longer
be counted as production latency work, and Phase 7's remaining value is now
purely the **prompt size** above, not the handshake it was originally about.

The memory pressure they were built for is likewise gone for now: the backend
sits at 225 MB of its 384 MB cap, with zero restarts and `OOMKilled=false` since
the 2026-09-17 deploy. The last cgroup OOM kill of daphne was 2026-09-16
12:27 — i.e. the deploy fixed it.

### Health baseline at the time of measurement

Recorded so a future "is it stuck?" has something to compare against. It was
not stuck: no errors, tracebacks, timeouts or cancellations anywhere in 48h of
backend logs, and `logs_executionlog` holds **no `running` rows** (237
completed / 116 failed / 1 cancelled; most recent agent run 2026-09-03). The
reported symptom was slow turns, not a hang.

---

## 4. Verification and Latency Benchmarking

To measure real-world impact after each phase, inspect the existing latency logger in [`chat/turn/agent.py:616`](file:///C:/Users/91700/Desktop/AIAAS/Backend/chat/turn/agent.py#L616):
```text
[Latency] it=0 tools=4ms(n=14) ttft=840ms total=1420ms prompt=1840 cached=1600(87%) out=82 model=anthropic/claude-3-5-sonnet
```
That line is an illustration, not a capture — note the retired model id. For a
real one, see §3d.

Since 2026-09-13 there is a second line per turn covering everything before the
graph, and it must be read alongside the first or the pre-model segment is
invisible again:
```text
[Latency] pre-model total=38ms intent=chat preflight=5ms ... vision_witness=9ms file_scope=3ms
```

* **Success Criteria** (revised 2026-09-18 against measured production, §3d):
  - ~~`tools_ms` drops from >3000ms to **< 50ms**~~ — **already true and never
    otherwise**: measured 6–35ms at `n=59`. Retired as a criterion.
  - `cached` becomes non-zero on `it>=1`. It is **0% on every call today**, and
    this is the one criterion that is both failing and ours to fix.
  - `prompt` on the first call of a fresh session drops below **12,000** tokens
    (Phase 7).
  - `ttft_ms` p95 drops below **5,000ms**. Measured spread is 2.0s–28.7s on one
    model; the tail is provider-side, so this is a routing criterion, not a code
    one.
  - Total turn completion time is reduced from the measured **30–83s** to
    **3–8s**.
