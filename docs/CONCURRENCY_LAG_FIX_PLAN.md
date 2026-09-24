# Concurrency Lag Fix Plan

**Status:** proposed 2026-09-24; Phases 0–5 built 2026-09-24, with Phase 5
done as option 2 (t3.small, 2 GB — operator step: resize the instance, then
`up -d`). Phase 6 is conditional on post-deploy measurements and is
deliberately not designed yet.
**Symptom:** the server lags badly when several things run at once — even for a
single user (a few chats open, an agent run, a coding lead with workers).
**Scope:** Backend + prod compose. No frontend change.

This plan comes from reading the code and the prod config, **not from
measurements on the box**. Phase 0 is therefore first: it confirms which gaps
are actually biting before anything is changed, and gives each later phase a
before/after number.

---

## 1. The gaps, in order of likely impact

| # | Gap | Where | Why it lags |
|---|-----|-------|-------------|
| G1 | **DB pool exhaustion** | `workflow_backend/background.py`, `DB_POOL_MAX_SIZE=10` | A `spawn()`ed task keeps its pooled connection until it *ends*, including the minutes it spends waiting on a model. Holders: the scheduler (for ever), each in-flight chat turn, each open chat SSE stream (a second one per turn), each agent run, each coding worker. One user with 3 chats + a lead and 3 workers needs ~11. Past 10, every new query waits up to `DB_POOL_TIMEOUT` (20 s). |
| G2 | **SQLite checkpointer in prod** | `AGENT_CHECKPOINTER=sqlite` in `docker-compose.prod.yml`; `chat/turn/checkpoints.py::_sqlite` | `AsyncSqliteSaver` has one `aiosqlite` connection and one `asyncio.Lock` for the whole process. Every super-step (4 per iteration) serialises the *entire* checkpoint (full transcript) with `serde.dumps_typed` **on the event loop**, then commits under that lock. Cost grows with transcript length and every run queues behind every other. LangGraph's own docstring: "not recommended for production workloads". |
| G3 | **One event loop, one core** | `Dockerfile` CMD: single `daphne` | 2 vCPU box, but Python runs on one core at a time. All users' streaming, parsing and serialisation share it. Cannot add workers yet: `chat/turn/runs.py::_runs`, the steering mailbox and `agents/agent/tasks.py` are in-process state. |
| G4 | **Memory overcommit / CPU credits** | `docker-compose.prod.yml` `mem_limit`s | Limits sum to ~1.3 GB (backend 384 + sandbox 300 + db 256 + redis/caddy/frontend 128 each) on 913 MB RAM + 1 GB EBS swapfile. Under load the box swaps. A 913 MB / 2 vCPU box is likely a burstable t3.micro — throttled to baseline once credits run out. |
| G5 | **Per-run single thread** | `chat/tools/fetch.py:132`, `chat/tools/media.py:254`, `chat/tools/office/__init__.py:114` | Each run's thread-sensitive `sync_to_async` calls share one thread (`ThreadSensitiveContext` = `ThreadPoolExecutor(max_workers=1)`). A 60 s download, an image generation or an office render on it blocks that run's ORM calls and serialises its "parallel" sibling tools. |
| G6 | **WebSocket DB work on the global thread** | `streaming/consumers.py`, `core/realtime/consumers.py`, `core/realtime/channels_middleware.py` | Channels 4.3 wraps consumers in no `ThreadSensitiveContext`, so every `database_sync_to_async` from every socket of every user falls through to asgiref's process-wide `single_thread_executor`. |
| G7 | **Chat checkpoints never pruned** (unverified) | `chat/turn/agent.py::forget_thread` is only called from agent/task paths | Chat keys the checkpointer by session id; the SQLite saver stores a full copy per super-step. The local file is too small to prove growth — check prod's `/app/data/checkpoints.sqlite3`. |

Already fine (checked, no action): chat token streaming is in-memory queues
(`runs.subscribe`); agent broadcasts are per step, not per token
(`agents/agent/stream.py`); the sandbox call is already
`thread_sensitive=False` (`sandbox/engine.py:57`); the KB idle sweeper sleeps
on its own daemon thread.

---

## 2. Phases

Each phase is independently shippable and measurable. **Phases 0–2 are one
deploy** and are expected to remove most of the lag.

### Phase 0 — Measure (before touching anything)

Two cheap instruments, same contract as `[Latency] pre-model`: one line,
grep-able, cheap to emit.

1. **Event-loop lag** — new `workflow_backend/loopwatch.py`: a task started
   next to the scheduler (`ensure_started`) that sleeps 1 s and logs
   `[Latency] loop-lag <ms>` when it wakes more than 100 ms late. This is the
   direct answer to "is something blocking the loop" (G2, G3).
2. **Pool pressure** — the same task, once a minute, logs
   `connection.pool.get_stats()` fields `pool_size`, `pool_available`,
   `requests_waiting`, `requests_wait_ms`, `requests_errors` (psycopg-pool).
   Skip silently when the pool is off (SQLite dev). This confirms G1.

On the box, once, with 3 chats + one agent run going:

```bash
free -m && vmstat 1 10                       # si/so > 0 => swapping (G4)
docker stats --no-stream
docker exec aiaas-db psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
  -c "select state, count(*) from pg_stat_activity group by 1"   # ~10 app conns => G1
docker exec aiaas-backend ls -la /app/data/checkpoints.sqlite3*  # G2 / G7
```

Plus CloudWatch `CPUCreditBalance` for the instance (G4).

**Record the numbers in §4 of this file** so every later phase has a baseline.

### Phase 1 — Release DB connections at the long waits (G1)

The rule: **a task must not hold a pooled connection across a wait it does
not control** (a model call, a stream, a sleep, a worker). With the pool,
`close_old_connections()` returns the connection to the pool (because
`CONN_MAX_AGE=0`, `close_if_unusable_or_obsolete` always closes), and the next
query takes one back in microseconds.

Add one helper so the rule has one spelling:

```python
# workflow_backend/background.py
async def release_db() -> None:
    """Hand this thread's DB connection back to the pool before a long wait.

    Must be awaited from the task that opened it — thread-sensitive
    sync_to_async runs on that task's own thread, which is the thread whose
    connection Django keyed.
    """
    await sync_to_async(close_old_connections)()
```

Call it at:

| Site | Wait it precedes |
|------|------------------|
| `chat/turn/agent.py::agent_node`, immediately before the model call | every model call — chat, agent runs, workers, one place |
| `chat/transport/streaming_http.py`, after `authenticate` / `get_session`, before returning `StreamingHttpResponse` | the whole SSE stream |
| `agents/scheduler.py::run_forever`, before `asyncio.sleep(TICK_SECONDS)` | the 30 s tick |
| `chat/tools/tasks.py::wait_tasks`, before it blocks on worker events (read the loop first and put it where it actually waits) | the lead waiting on workers |
| `streaming/broadcaster.py` execution SSE generator, before its wait loop | the run-watch stream |

Two cautions:
- Never call it inside `transaction.atomic()` — Django will not close there.
  None of the sites above are, but the test below proves it.
- Do **not** raise `DB_POOL_MAX_SIZE` instead. It hides G1 and spends Postgres
  memory (~5–10 MB per backend in a 256 MB container).

**Tests** (`workflow_backend/tests/test_release_db.py`, runs only when the
pool is configured — skip on SQLite):
- a stubbed chat turn whose model call awaits an `asyncio.Event`; while
  blocked, `pool.get_stats()['pool_available'] == pool_size` (nothing held);
- 15 concurrent stubbed turns with `max_size=4` all complete, none waits
  `DB_POOL_TIMEOUT`.

### Phase 2 — Postgres checkpointer (G2)

The package is already in the image (`langgraph-checkpoint-postgres==3.1.2`,
`psycopg-pool`), and `checkpoints._postgres` exists. Changes:

1. `checkpoints._postgres`: `max_size=10` →
   `int(os.environ.get('AGENT_CHECKPOINT_POOL_MAX', '4'))`, `min_size=1`.
   Budget: app pool 10 + saver 4 + psql/migrations headroom ≤ 25
   (`max_connections`). Write this sum into the compose comment.
2. Confirm `setup()` creates the tables on first boot (`AsyncPostgresSaver.setup`)
   — the recovery sweep already calls `checkpoints.setup`.
3. `docker-compose.prod.yml`: `AGENT_CHECKPOINTER: "postgres"`; drop
   `AGENT_CHECKPOINT_PATH`. `AGENT_CHECKPOINT_DSN` falls back to
   `DATABASE_URL` — confirm `.env` sets `DATABASE_URL` (not only `DB_*`); if
   not, set `AGENT_CHECKPOINT_DSN` explicitly.
4. Cut-over: before `up -d`, check
   `ExecutionLog.objects.filter(status__in=['running','paused']).count() == 0`
   (as on 2026-09-17). Anything paused must be answered first — its state is
   in the SQLite file and will not follow. Keep the SQLite file on the volume
   for a week, then delete.

Why it is faster, not just "a better database": no process-wide lock, and the
Postgres saver stores channel blobs **per version**, so an unchanged transcript
is not rewritten every super-step — the SQLite saver writes a full copy each
time.

**Tests:** existing `agents/tests/test_recovery.py` +
`logs/tests/test_checkpoints.py` pass against a Postgres saver
(`AGENT_CHECKPOINTER=postgres` in a one-off CI/local run with Postgres).

**Deploy** per `DEPLOYMENT.md` + the `project_prod_deployment` procedure:
dated tag, `pg_dump -Fc` first, smoke-test the image, verify health 200 and one
chat turn end to end.

### Phase 3 — Keep slow work off the run's thread and the loop (G5, G6)

Rule: **anything that touches the ORM stays thread-sensitive; pure I/O or CPU
work goes to `thread_sensitive=False`** (the default executor pool).

| File | Change |
|------|--------|
| `chat/tools/fetch.py` | `_fetch` → `sync_to_async(_fetch, thread_sensitive=False)` (pure HTTP). The `write_file`/`write_binary` calls stay as they are. |
| `chat/tools/media.py` | `_image_bytes` → `thread_sensitive=False`. `_generate` mixes HTTP with a scope write: split so the HTTP half runs off-thread and the save stays on it. |
| `chat/tools/office/__init__.py::_save` | Split render (CPU, python-pptx/xlsxwriter/docx) from `vfs.write_binary` (ORM). Render off-thread, write on-thread. |
| `streaming/consumers.py`, `core/realtime/consumers.py`, `imagine/consumers.py` | A small base consumer that runs `websocket_connect`…`disconnect` inside one `ThreadSensitiveContext` and calls `close_old_connections` on disconnect — each socket gets its own thread instead of the global one. |
| `core/realtime/channels_middleware.py` | The JWT user lookup runs inside that context too (move it into the consumer base, or wrap the middleware's `__call__`). |

Optional, only if Phase 0's loop-lag line still shows spikes after Phase 2:
wrap the saver so `serde.dumps_typed` runs in `asyncio.to_thread`.

**Tests:** a tool test that blocks `_fetch` on an event while a sibling
`parallel=True` tool with an ORM call completes (proves they no longer queue);
a consumer test asserting two sockets' DB calls run on different threads.

### Phase 4 — Prune chat checkpoints (G7)

Only after Phase 0 shows it matters. On Postgres: a sweep that keeps the
latest N (e.g. 3) checkpoints per chat thread (`thread_id` not starting with
`agent-`) and deletes older rows + their `checkpoint_writes` /
`checkpoint_blobs`. A new turn reads only the latest. Hook it into the
existing `recover_runs` / recycle sweep cadence rather than adding a loop;
reachable as beat task and management command like every other sweep.

### Phase 5 — The box (G4) — config only

In order of cost:
1. If `CPUCreditBalance` sits near 0: enable T3 **unlimited** or move off
   burstable. No code change matters while throttled. (Operator step —
   AWS console, not built.)
2. Move to **2 GB (t3.small)**. The current limits promise more memory than
   exists, so swapping under load is by design. (Applied 2026-09-24:
   compose resized for 2 GB — sandbox 600m / two slots / 256 MB per run,
   everything else unchanged, caps totalling ~1.6 GB. Needs the instance
   resize + `up -d` to take effect. Supersedes option 3 below.)
3. If staying on 913 MB: sandbox `mem_limit` 300m → 200m with
   `SANDBOX_MEM_MB=160`, and let the sidecar run one execution at a time.
   (Superseded by option 2 above — kept as the fallback if the box stays.)

### Phase 6 — Only if still CPU-bound: more than one process (G3)

Not before Phases 1–5, and not on 913 MB (a second Daphne is ~200 MB). The
blocker is in-process state:
- `chat/turn/runs.py::_runs` (frames + listeners for reattach),
- `chat/turn/steering.py` mailbox,
- `agents/agent/tasks.py` task registry.

Either move frames/steers to Redis (already running for the channel layer), or
pin each user to one worker (sticky routing by user in Caddy). Design this as
its own plan when needed.

---

## 3. What not to do

- **Don't raise pool sizes to make G1 go away.** It moves the failure into
  Postgres memory and hides the hold-across-wait bug.
- **Don't add Daphne/uvicorn workers first.** Steering and stream reattach
  break silently when a request lands on the worker that doesn't own the run.
- **Don't blanket `thread_sensitive=False`.** Django connections are
  per-thread; ORM on the shared default pool leaks connections and breaks
  transactions. Only pure I/O/CPU work moves.
- **Don't keep the SQLite checkpointer "because it works in dev".** It is the
  dev backend by this module's own description.

---

## 4. Measurements

Fill in from Phase 0, then after each phase.

| Metric | Baseline | After P1+P2 | After P3 |
|--------|----------|-------------|----------|
| `loop-lag` p95 (ms), 3 chats + 1 run | | | |
| pool `requests_waiting` peak | | | |
| pool `requests_wait_ms` / min | | | |
| app connections in `pg_stat_activity` | | | |
| swap in/out (`vmstat` si/so) | | | |
| backend RSS (MB) | | | |
| chat time-to-first-token with 1 agent run going (s) | | | |
