# Orchestrator Response Time Optimization Plan

## 1. Executive Summary

This plan addresses slow response times in the orchestrator and agent run execution pipeline (`agents/agent/orchestrator.py`, `agents/agent/runtime.py`, `chat/turn/agent.py`). 

Profiling reveals four major architectural bottlenecks contributing to high latency (often 15s to 45s+ per turn):
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

## 4. Verification and Latency Benchmarking

To measure real-world impact after each phase, inspect the existing latency logger in [`chat/turn/agent.py:616`](file:///C:/Users/91700/Desktop/AIAAS/Backend/chat/turn/agent.py#L616):
```text
[Latency] it=0 tools=4ms(n=14) ttft=840ms total=1420ms prompt=1840 cached=1600(87%) out=82 model=anthropic/claude-3-5-sonnet
```
* **Success Criteria:**
  - `tools_ms` drops from >3000ms to **< 50ms**.
  - `ttft_ms` drops by **40–60%** on intermediate tool turns.
  - Total turn completion time is reduced from **15–40s** to **3–8s**.
