# Engineering decisions

The problems in this project that took more than one attempt, what was decided,
and why. Each section links to the code and tests that hold the decision in
place. The last section is an honest account of where the system stops scaling
today and what the next step for each limit is.

---

## 1. A production out-of-memory kill, and budgeting in megabytes

**Problem.** On 2026-09-16 the web server was killed by the kernel inside a
384 MB container. One chat turn had asked all eight connectors which tools they
offered, and each answer meant a cold `npx` start, two Node processes, 70–150 MB
each. The existing session pool had a *count* cap, which couldn't help: the
processes that exhausted memory didn't exist yet when the cap was checked.

**Decision.** Three separate fixes.

- **Listing never starts a connector.** Which tools exist is a catalogue
  question. The catalogue is stored in the database (`MCPToolCatalogue`) and
  served from there; a connector process starts only when a tool is *called*.
  Refreshes drain through one serial queue, not a task per connector.
- **Admission is by megabytes, not by count.** `mcp_integration/supervisor.py`
  reserves an estimated footprint *before* spawning, evicts idle
  least-recently-used sessions to make room, and refuses cleanly when nothing
  can be freed. It then measures `/proc` so the next estimate is real. There
  is also a backstop checked against the container's cgroup limit, because a
  percentage threshold answers one start too late.
- **Half the processes.** `launch.py` rewrites `npx -y <pkg>` into a direct
  `node` launch when the package is preinstalled in the image, removing a
  launcher process that sat idle for the life of every session.

**Degrades to permissive.** No `/proc` means falling back to counting; a
budget of 0 disables the ceiling. Losing the ability to measure must not take
away the ability to run.

Tests: `mcp_integration/tests/test_supervisor.py`, `test_listing_never_spawns.py`, `test_launch.py`.

## 2. Human approval that doesn't run anything twice

**Problem.** LangGraph's `interrupt()` pauses a run by discarding the node's
writes and *re-running the node from the top* on resume. If the tool node
checked permissions inline, a batch of three calls (two safe, one needing
approval) would run the two safe calls, pause, and run them *again* on resume.
Graph state rolls back; an email that was sent doesn't.

**Decision.** The tool node runs in passes: settle every approval gate first,
then dispatch. Nothing leaves the process until every gate in the batch is
answered. The approval record (`HITLRequest`) is idempotent on
`(execution, call_id)`, because the replayed node would otherwise open a second
request and a second reminder ladder for the same question.

Approval is a **policy over the call, not a list of names**: connector tool
names are minted at runtime by third parties, so a name list can never contain
them. A credentialed connector call is gated unless its name starts with a
read-only verb. The allowlist runs that way round on purpose: guessing "write"
costs a click, guessing "read" sends the email.

Users get five autonomy levels, because a system offering only "ask every time"
and "never ask" trains people to pick the second one once and stop reading. The
middle level gates on each tool's declared *effect* (`read | reversible |
irreversible`, defaulting to irreversible), not on its name.

Tests: `agents/tests/test_autonomy.py`, `chat/tests/test_permissions.py`, `agents/tests/test_hitl_inbox.py`.

## 3. Forty-iteration agent runs inside a context window

**Problem.** An agent run resends its whole transcript every iteration. The
original trimmer dropped one *message* at a time, so it could remove an
assistant turn while keeping the `tool` results that answered it. Providers
reject a tool result that refers to a missing call with a 400, so long runs
didn't degrade, they died. It also counted only `content`, and tool-calling
messages keep their payload in `tool_calls[].arguments`, so the largest entries
scored as zero.

**Decision.**

- The unit of trimming is the **segment**: an assistant tool-call turn plus its
  results, which is indivisible (`llm/budget.py`).
- Curation fires at a **watermark** (70% of the budget, cutting to 45%) rather
  than a little every turn. Rewriting the request prefix on every call would
  forfeit provider prompt caching, the same reason the clock was moved *out* of
  the system prompt.
- What is cut **stays reachable**: it's archived, and the model gets a notice
  naming an id it can fetch. With archiving off, the notice says the text is
  gone rather than naming an id nobody wrote.
- The agent's **plan lives outside the transcript** (`chat/turn/todos.py`),
  in graph metadata, which curation never touches. Otherwise by iteration 30 the
  original instruction has been summarised away and the agent is working from
  a compressed trace of its own footsteps.

Tests: `chat/tests/test_curation_e2e.py` runs twenty real turns through the
real graph against a stub provider and asserts on what was actually sent.

## 4. Parallel tool calls, safely

**Problem.** A model issues every call in a turn before seeing any result, so
calls within one turn are independent. They were still dispatched one at a
time, so three web searches took three round trips.

**Decision.** Safe calls run with `asyncio.gather`. Parallelism is an
**allow-list** declared per tool, because the unsafe cases can't be seen from a
name: the Python sandbox's in-process fallback swaps the process-global
`sys.stdout`, and connector tools are unknown so they run serially. Results are
**recorded in call order**, not completion order, so the transcript and the run
log are identical across reruns of the same turn. The prompts were also changed
to tell the model to batch, because the engine is useless if the model calls
one tool, waits, then calls the next.

Tests: `chat/tests/test_parallel_tools.py`.

## 5. Fail before looking busy

**Problem.** A turn with no valid credential or no credit would show "thinking",
persist the message, load history, and then render an apology *in the
assistant's voice* for a problem only the user could fix.

**Decision.** `llm.preflight()` runs before the first status event and before
anything is written. Provider errors are classified into typed exceptions
(access denied, quota exhausted, model retired) that callers *raise* rather than
render. Agent runs preflight too, so the API answers **402** naming the provider
instead of 202 followed by a dead run.

Credits hook into the same door (`llm/credits.py`): checked at preflight,
charged once the provider reports usage, only on the platform's own key, and
never on free models. Charging is a single `UPDATE ... SET credits = GREATEST(credits - n, 0)`,
so concurrent calls from a fan-out subtract correctly instead of overwriting
each other.

Tests: `chat/tests/test_account_errors.py`, `llm/tests/test_credits.py`.

## 6. Security boundaries that hold by construction

- **Connectors get an allow-listed environment.** A stdio connector is
  third-party code we spawn. Its environment used to be `{**os.environ, ...}`,
  which in Docker is the whole `.env`, *including the master key for every
  user's credential vault*. It's now `PATH`, a few prefixes and the one user's
  mapped credentials (`mcp_integration/tests/test_subprocess_env.py`).
- **Code runs in a sidecar**, not in the web process: its own container, no
  network, all capabilities dropped, read-only root, memory and pid caps,
  process-group kill on timeout. A full interpreter escape lands in a container
  holding no secrets (`docs/SANDBOX_EXECUTION.md`).
- **The agent file system is virtual.** Paths are walked segment by segment
  over database rows; `os` is never imported, so a traversal bug can reach
  another row but never a host file. Unknown and foreign ids get the *same*
  404, so the API can't be used to check whether an id exists.
- **Unauthenticated surfaces answer 404 for every refusal** (webhooks, public
  agents), so they can't be used to find out which secrets or slugs are live.
- **Shared agents carry requirements, never ids.** A published config that
  named "knowledge base 2" would, when installed elsewhere, silently read
  *someone else's* row 2. Publishing strips ids through an allow-list
  projection, so a field added later is private until someone decides otherwise.

## 7. Testing what users actually receive

Twice, a feature passed every unit test and reached no user. A todo list was
built, streamed and stored, but no component rendered it. Each unit test
covered one hop, which lets a chain pass everywhere while connecting nowhere.
`chat/tests/test_turn_output_e2e.py` drives the real graph and asserts on the
two things a client consumes: the events emitted and the metadata left behind.
It found a real bug on its first run.

---

## Where it stops scaling today

Current deployment: one 1.9 GB EC2 instance behind Cloudflare, running the web
server, Celery worker and beat, Redis, the sandbox, and **SQLite** on a Docker
volume. That's the right cost for early access, but it has clear limits.

| Limit | Why | Next step |
|---|---|---|
| **One web process holds live run state** | Active chat runs (`chat/turn/runs.py`) and the steering mailbox (`chat/turn/steering.py`) are in-process dicts. With two replicas, "stop" or "steer" can reach the replica that isn't running the turn. | **Short term:** sticky sessions at the load balancer (route by user), which needs no code, since run *state* is already durable in the checkpointer. **Long term:** Redis-backed mailbox and a Redis pub/sub cancel signal, with the event stream written to a Redis stream so any replica can re-attach. |
| **SQLite** | One writer at a time; every write in a turn queues behind every other. | Move to PostgreSQL + PgBouncer. Settings, the Postgres checkpointer and a migration plan (`docs/POSTGRES_PRODUCTION_MIGRATION_PLAN.md`) already exist. |
| **Single instance** | The box is a single point of failure; a hardware loss loses data. | Nightly `manage.py backup_db` to S3 now; a managed database later, so the app tier can be replaced freely. |
| **Connectors are memory-bound** | Each connector session is a 70–150 MB process, so concurrent connector users scale with RAM, not CPU. | Native REST implementations for the most-used connectors (Gmail, Drive, Sheets, Calendar are already native); move remaining stdio connectors to a separate host. |
| **Pre-model latency** | Up to 20 sequential awaits before the first token, each a thread hop onto a locked SQLite. Now measured per turn (`[Latency] pre-model`). | Postgres removes the lock; batch the pre-model reads; stop seeding a web search for questions that don't need one. |

What already works across processes: run state (durable checkpointer),
orphaned-run recovery (`agents/recovery.py`), the cache (Redis), WebSocket
fan-out (Redis channel layer), and every periodic job, each reachable both as a
Celery beat task and as a management command.
