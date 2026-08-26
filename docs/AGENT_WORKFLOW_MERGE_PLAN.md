# Merging the workflow app and the agent app — implementation plan

> ** Deprecated — superseded 2026-08-14, kept as a historical record.**
> All seven stages shipped on 2026-08-14, and the workflow canvas they served
> was retired with the DAG product on 2026-08-16/17. The current plan is
> [AGENT_BLOCKS_PLAN.md](AGENT_BLOCKS_PLAN.md); what survived the retirement
> and what was deleted is [WORKFLOW_RETIREMENT.md](WORKFLOW_RETIREMENT.md).

Written 2026-08-14. This is the **implementation plan for the merge**.
The architecture behind it, and the orchestrator tier it grows into, live in
`AGENT_WORKFLOW_UNIFICATION.md` — that doc is design, this one is the build.

## The goal, restated

Three surfaces, one system:

| Surface | Job | Today | After |
|---|---|---|---|
| Agents page | **author** an agent | `AgentBuilder.tsx` | unchanged, plus a link to its canvas |
| Canvas | **visualize / debug** | workflows only | workflows **and** agent runs |
| Left sidebar | **connect / configure** | inserts DAG nodes | inserts nodes *or* configures the agent |

**Not in scope, deliberately:** `Goal` / `GoalStep` / `ContextEntry`, the
orchestrator tier, multi-agent handoff. The merge is independently valuable and
ships without any of it. §7 lists the four cheap constraints that keep the
orchestrator from forcing a rewrite later.

## The key finding

The canvas already reads a specific contract off `ws/execution/{id}/`
(`WorkflowEditor.tsx:758-830`):

```
{type: 'execution.event', data: {event_type: 'node_started',  data: {node_id, input}}}
{type: 'execution.event', data: {event_type: 'node_complete', data: {node_id, output, status, error}}}
```

That is the *whole* contract. An agent run already opens an `ExecutionLog` with
an `execution_id` (`agent_runtime.py::_open_log`) and already emits a sink event
per tool call. **Nothing translates one into the other — that missing translator
is most of the merge.** Once it exists, live agent runs light up the existing
canvas with no new frontend socket code.

---

## Stage 1 — An agent run emits what the canvas already reads **DONE**

*Backend only. Verifiable with no UI.*
*Shipped 2026-08-14. `agents/agent/stream.py`, `agents/tests/test_agent_canvas.py`.*

1. **`agents/agent/stream.py`** (new) — an `EventSink` that translates and
   broadcasts to group `execution_{execution_id}`:
   - `AGENT_TRACE` → `node_started`, `node_id` = the tool call id
   - tool result → `node_complete` with `output`, `status`, `error`
   - `ASK_PERMISSION` → `hitl.request` (the group the canvas already listens on)
   - `ERROR` / `DONE` → terminal `execution.event`
2. **`agent_execute` goes detached.** It is synchronous today
   (`agents.py:456`) and returns only at the end, so streaming is impossible.
   Return `202` + `execution_id` immediately, run via
   `workflow_backend.background.spawn()` — **never** `asyncio.create_task`
   (CLAUDE.md: `CurrentThreadExecutor already quit`).
3. **Persist the trace.** Write one `NodeExecutionLog` per tool call
   (`node_id`, `node_type`=tool name, `execution_order`, `input_data`,
   `output_data`, `status`, `duration_ms`, `error_message`). Without this the
   canvas can show a live run but not reopen a finished one.
   Threaded through a new optional `TurnContext.on_tool_result` hook so chat
   turns are unaffected.
4. Update `docs/API.md` for the changed `agent_execute` response.

**Done when:** running an agent produces `node_started` / `node_complete` frames
on the execution socket, and a finished run leaves `NodeExecutionLog` rows.

## Stage 2 — Project a run into a graph **DONE**

*Backend only.*
*Shipped 2026-08-14. `agents/agent/graph_projection.py`.*

1. **`agents/agent/graph_projection.py`** (new). One entry point, returning a
   discriminated shape:
   ```
   {mode: 'agent_run' | 'agent_idle' | 'workflow', nodes: [...], edges: [...]}
   ```
 - `agent_run` — **superseded, see the deviations below.** As written this
     said "one node per row, chained by `execution_order`". That drawing is a
     workflow, not an agent, and shipped misleading. What is built is a loop:
     a spine of *think* nodes with calls fanning out and results fanning back.
   - `agent_idle` — the capability map: the agent, its granted tools,
     connectors, KBs, skills. This is the default view before a first run.
2. **Endpoints** (+ `docs/API.md` rows in the same change):
   - `GET /orchestrator/agents/{id}/graph/` — idle capability map
   - `GET /orchestrator/agents/{id}/runs/` — run history
   - `GET /orchestrator/executions/{eid}/graph/` — a run's trace

**Done when:** both endpoints return ReactFlow-shaped JSON the existing canvas
can render unmodified.

**Three deviations from the plan, all deliberate:**

1. **The trace is a loop, not a chain — the plan was wrong here.** It called for
   "one node per tool call, edges by `execution_order`". That shipped first and
   was **misleading**, which is the one thing a debugging view cannot be: a
   chain is what a *workflow* is, and drawing an agent that way claims that call
   2 waited on call 1 and consumed its output. Neither is true. The projection
   now draws what actually happens — a spine of *think* nodes, one per turn of
   the model, with its calls fanning out as siblings and their results fanning
   back into the next turn:

   ```
   [Goal] → [Thinks · turn 1] ⇉ web_search ⇒ [Thinks · turn 2] ⇉ python ⇒ [Answer]
                              ⇉ read_url   ⇗
   ```

   This required persisting `iteration` per step (`config['iteration']`), which
   the runtime already knew and was discarding. Rows written before that fall
   back to one turn per step — a narrow loop, but never a false claim about
   parallelism. Pinned by `test_a_run_is_drawn_as_a_loop_not_a_chain` and
   `test_calls_in_one_turn_are_siblings_not_a_sequence`.

2. **Provenance edges were not built** — and are now largely moot. The loop
   already shows where a result went (back into the model). Inferring "step 3
   consumed step 1's output" from argument text stays deferred; a wrong
   dependency edge is worse than no edge.

3. **Resume-on-approve was added, unplanned.** Approving only wrote consent
   into the checkpoint; the paused run had already returned, so nothing picked
   it up and the user approved into silence. `resume_agent_run` continues the
   run on its *original* execution id — a resumed half arriving on a second id
   would split the trace across two runs the canvas cannot join.

## Stage 3 — Extract the execution stream (no behaviour change) **DONE**
*Shipped 2026-08-14. `lib/executionEvents.ts` + 27 tests; `WorkflowEditor` now consumes them.*

*Frontend only. The riskiest step — do it alone and verify before Stage 4.*

**Revised 2026-08-14, after finding the frontend has no test runner** — no
vitest, jest, testing-library or playwright; `tsc -b` and eslint are the only
gates. The original plan was to extract a whole `CanvasShell` out of
`WorkflowEditor.tsx` (1626 lines) and verify by eye. That is a bad trade
against no safety net: typecheck cannot catch a broken socket handler or a
dropped effect dependency, which is exactly how this code fails.

Narrowed to the part that is both **risky and testable**:

1. Add `vitest` — the frontend has no way to assert behaviour at all today,
   and every later stage is worse off for it.
2. Extract the execution-event handling (`WorkflowEditor.tsx:758-931`) into a
   **pure reducer**, `applyExecutionEvent(nodes, event) -> nodes`, plus a thin
   `useExecutionStream` hook that owns the socket.
3. **Test the reducer against the literal wire shapes** the backend emits.
   This is the same contract `agents/tests/test_agent_canvas.py` pins from the
   other side, so the two suites now meet in the middle — a rename on either
   side fails a test instead of silently blanking the canvas.
4. `WorkflowEditor` consumes the hook. Everything else about it is left alone.

The full `CanvasShell` extraction is **not abandoned** — it is deferred until
there is coverage to refactor against. Stage 4 does not need it: the agent
canvas can consume the same hook without the two pages sharing a shell.

**Exit condition:** workflows behave exactly as before — build, save, autosave,
run, watch nodes light up, inspect outputs, version history, import/export.

## Stage 4 — Agent mode on the canvas **DONE**
*Shipped 2026-08-14. `pages/AgentCanvas.tsx`, `components/workflow/AgentTraceNode.tsx`,
route `/agents/:id/canvas`, cross-links from the agent list.*

1. `CanvasShell` takes `mode: 'workflow' | 'agent_run' | 'agent_idle'` — an
   enum from day one, not a boolean (§7).
2. `useGraphStore` gains `mode` and `topologyEditable`. **Enforced in the store**
   — `onConnect` / `addNode` reject when topology is not editable. A UI-only
   guard would let some other code path silently author an agent graph.
3. `AgentTraceNode` — reuses `GenericNode`'s status styling and
   `ExecutionOverlay`'s badges; shows tool name, arg summary, duration, and the
   model's `thought` for that step.
4. Route `/agents/:id/canvas`; cross-links both ways with `AgentBuilder`.
5. Click a trace node → `NodeConfigPanel`'s output viewer, reused, showing exact
   input/output JSON and the error.
6. Approve a paused sensitive tool from the canvas — the existing HITL UI, since
   Stage 1.1 already bridged `ASK_PERMISSION` onto that channel.

## Stage 5 — The sidebar configures the agent **DONE**
*Shipped 2026-08-14. `components/workflow/CapabilityPanel.tsx`.*

*This is the specific ask: "the connector side bar looks good — make it give
options to configure their agent."*

`NodePanel.tsx` becomes mode-aware and keeps its shelf/search/category layout:

| mode | shelf lists | clicking an item |
|---|---|---|
| workflow | node types (`useNodeTypes`) | inserts a node |
| agent | connectors, tools, KBs, skills | toggles a grant on the agent |

- Connector metadata comes from `Connectors.tsx`'s `CONNECTOR_META`; tools from
  `agentConfig.ts`'s `tools` shape; KBs and skills from `kbService` /
  `skillsService` — all already exist.
- Toggling writes through `agentsService.update` (PATCH, so an unsent knob keeps
  its value — narrowing vs. widening a permission).
- The item's granted state is visible on the shelf, so the sidebar reads as the
  agent's current configuration, not a menu.

## Stage 6 — One list, one navigation **DONE**
*Shipped 2026-08-14. `Sidebar.tsx` has one "Automations" entry, and its primary
button creates an automation (`/agents/new`) rather than a bare workflow.*

**Revised 2026-08-14 (second pass).** The first pass kept both lists behind a
shared `AutomationTabs` strip. That still presented the workflow catalogue as a
destination, which it is not — you open a workflow from the agent that owns it,
from a template, or from a run you are debugging, not by browsing a list. So the
list page is gone rather than tabbed.

- `WorkflowsDashboard.tsx`, `AutomationTabs.tsx` and `components/workflows/`
  (`WorkflowCard`, `WorkflowIcon` — used only by that page) are **deleted**.
- `/workflows` redirects to `/agents`. `/workflow/:id` and `/workflows/new`
  still resolve, so every deep link, template deploy and Overview link opens the
  canvas as before.
- The sidebar CTA was "New workflow" (POST a blank workflow, jump to the
  editor); it is now "New automation" → `/agents/new`.
- `ErrorBoundary`’s "go home" pointed at `/workflows`; it now goes to `/overview`.

**Standing deviation:** the two lists were never flattened into one grid, and
now there is only one — `/agents`. The workflow-specific list features (cursor
pagination, status filters) went with the page; if they are wanted back they
belong as filters on the automations list, not as a second catalogue.


- Merge `WorkflowsDashboard.tsx` and `Agents.tsx` into one list with kind tabs
  (All / Agents / Workflows), reusing `WorkflowCard` with an agent variant.
- `Sidebar.tsx:147-148` currently has two entries; collapse to one
  ("Automations"), keeping `/workflows` and `/agents` as filtered views so
  existing links and bookmarks keep working.
- `/workflow/:id` and `/agents/:id` keep resolving; the editor picks its surface
  from `kind`.

## Stage 7 — Verification

Run at the end, as asked, but each stage above also has its own exit condition.

**Backend** — `pytest`:
- `agent_stream`: a tool call produces exactly the `node_started` /
  `node_complete` shape the canvas parses (assert against the literal keys —
  this contract is the merge)
- `NodeExecutionLog` rows written per tool call, ordered, with errors captured
- `agent_execute` returns 202 + `execution_id` without blocking; the detached
  run survives the response (regression for the `spawn()` rule —
  `workflow_backend/tests/test_background.py`)
- projection: idle, mid-run, failed, and paused runs each produce valid
  `{mode, nodes, edges}`; ambiguous provenance falls back to a sequential chain
- ownership: another user's agent id 404s on all three new endpoints
- Known env gotcha: `orchestrator/` failures are often missing-whitenoise, not
  regressions; `nodes/` is the clean signal.

**Frontend:**
- `npx tsc -b --force` (plain `tsc --noEmit` checks zero files — root tsconfig
  is references-only)
- `npm run lint`, `npm run build`
- endpoint-wiring check: resolve every new `apiClient` path against the Django
  URLconf — catches dead wiring typecheck and pytest both miss

**End-to-end, manually:**
1. Existing workflow still builds, saves, runs, and lights up — *the Stage 3
   regression check, and the one most likely to break*
2. Build an agent in `AgentBuilder` → open its canvas → capability map renders
3. Configure it from the sidebar → reopen the builder → the knob agrees
4. Run it → nodes appear and light up live
5. It pauses on a sensitive tool → approve from the canvas → it continues
6. Reopen the finished run from history → same graph, no socket

---

## Sequencing

Stages 1–2 (backend) and Stage 3 (the extraction) are independent and can run in
parallel. Stage 4 needs all three. Stages 5 and 6 are independent of each other.

```
1 ─┐
2 ─┼─> 4 ─> 5 ─┐
3 ─┘      6 ─┴─> 7
```

The single highest-risk item is Stage 3. It touches working code and delivers no
visible feature, which makes it the one most tempting to merge into Stage 4 —
don't. Every later stage assumes it succeeded.

## §7 — Four constraints that keep the orchestrator cheap later

None of these cost anything now; all four are expensive to retrofit.

1. **`mode` is an enum, never a boolean.** `'orchestration'` becomes a fourth
   value instead of a refactor of every call site.
2. **The projection returns a discriminated shape** (`{mode, nodes, edges}`)
   from day one, so one renderer already handles multiple graph kinds.
3. **Events carry a root execution id**, even when a run is its own root. When
   agent runs later nest under a goal, the client keeps one subscription and the
   fan-up has somewhere to go.
4. **`NodeExecutionLog.execution` already points at an `ExecutionLog` that has
   `parent_execution`.** Use the FK properly now; nesting then needs no data
   migration.


---

## Shipped state, 2026-08-14

**All seven stages are built and verified.** The merge is complete as planned.

**Verification actually run:**

| Gate | Result |
|---|---|
| `pytest` (backend, whole suite) | **793 passed**, 17 skipped, 0 failed |
| `vitest` — `executionEvents.test.ts` | **27 passed** (the new contract tests) |
| `vitest` — whole suite | 78 passed, 5 failed — **all pre-existing**, see below |
| `tsc -b --force` | clean |
| `eslint` on new files | clean |
| `npm run build` | succeeds |
| endpoint wiring | all 7 client paths resolve against the Django URLconf |

### Three findings that were not part of the plan

1. **The frontend had no test runner.** No vitest, jest, testing-library or
   playwright — but it *did* have orphaned test files that could never run.
   Installing vitest surfaced them. Two (`tests/integration/auth.test.tsx`,
   `Connectors.test.tsx`) need `msw`, which is not installed; adding a
   dependency is a call for the owner, so they are left failing rather than
   silently excluded.
2. **`isDAG` stack-overflows on a ~5000-node chain.** There is already a test
   for exactly this (`workflow-validation.test.ts`, "does not stack-overflow on
   5000-node chain") — it has simply never been run. Left red on purpose: it is
   a real bug in workflow validation, unrelated to this merge, and hiding it
   would waste the fact that it finally surfaced.
3. **Four assertions describe behaviour the validator no longer has** —
   `credential_id` is rejected where the test expects it accepted, zero and
   negative timeouts are not rejected, invalid subworkflow `input_mapping` JSON
   is not flagged. Each is either a real regression or a stale test. Relaxing
   them to match today's behaviour would rubber-stamp whichever ones are bugs,
   so they are left failing for a deliberate decision.

The shape drift in the same file (7 failures where `isDAG`/`hasTrigger` had
grown from returning a boolean to returning an object) *was* fixed — that one is
unambiguous.

### What a reviewer should check by hand

Nothing above exercises a browser. The manual pass in §Stage 7 still stands, and
its first item is the important one: **an existing workflow still builds, saves,
runs, and lights up.** Stage 3 rewrote the code path every workflow run depends
on, and no automated gate here covers the React wiring around it.


---

## Follow-up fixes, 2026-08-14 (after review)

Three defects found by the owner reviewing the shipped result. All fixed and
verified; recorded because each was a judgement error worth not repeating.

1. **The trace was drawn as a chain.** See the Stage 2 deviations above. The
   single most important fix here: a debugging view that misrepresents the thing
   it debugs is worse than no view.
2. **`CapabilityPanel` listed 5 of 7 tools** — `shell` and `fileOps` were
   omitted on the reasoning that the runtime refuses them. Wrong: an agent can
   still *hold* those grants, so the panel silently misstated what that agent
   was configured to do — the exact permissions drift `agents/views/agents.py`
   exists to prevent. Both restored, flagged `not served` when granted. The
   invariant (this list == backend `TOOL_KEYS`) is now written in the file.
3. **The agent canvas had no run log.** The workflow editor has had one at the
   bottom since forever; the agent canvas shipped without, so a failure was only
   a red border on a node you had to find and click. `AgentRunLog` added, with
   the failure count on the *collapsed* bar — an error you have to open
   something to discover is an error the user does not see.

### On `compiler/` — asked during review, answered here

Do not delete it. It is imported from 29+ sites across `executor/`, `nodes/`,
`orchestrator/`, `mcp_integration/`, `inference/` and `chat/`. But the value is
very unevenly distributed:

| Module | Lines | Non-test importers |
|---|---|---|
| `schemas.py` (`ExecutionContext`) | 460 | **20 distinct files** |
| `validators.py` | 449 | `orchestrator/views`, `executor/trigger_manager`, `executor/test_generator`, `executor/tasks` |
| `config_access.py` | 59 | `executor/{credential_utils,tasks,trigger_manager,test_generator}` |
| `compiler.py` — the actual ReactFlow→LangGraph translation | 588 | **1** — `executor/engine.py` |

So most of the app's gravity is its *schemas and validators*, which have nothing
to do with compiling; the compiler proper has a single consumer.

Extracting the schemas into their own app would be a 29-file rename for a naming
win; not worth the churn now. And `compiler.py` gets *more* valuable under the
agent-first model, not less: when `run_workflow` lands as a granted agent tool
(`AGENT_TEMPLATES.md` §7), it becomes the thing that guarantees a side effect
happens identically every time. The agent decides *whether*; the compiler is how
the *how* stays deterministic.


---

## Verified in the running app, 2026-08-14

Everything above is tests. This section is the app actually running: Django on
:8000, Vite dev server, a real agent ("Code review agent", `shell` granted), and
Chromium driven with Playwright.

**What running it proved that the tests did not — three defects, all found here:**

1. **`think-N → think-N` self-loops.** Every turn after the first pointed at
   itself: the spine edge was drawn from a `previous` marker the result edges
   had already advanced to that same node. The unit tests all asserted certain
   edges were *present* and never that nonsense was *absent*, which is exactly
   the gap a self-loop slips through. Fixed; `test_no_node_points_at_itself` and
   `test_every_edge_joins_two_real_nodes` now close it.
2. **Capabilities overlapped, and the map read as a chain.** Each family got its
   own column with rows restarting at zero, so the first tool and the first
   skill shared a `y` — and the `agent → skill` edge ran straight *through* the
   tool node. On screen that reads "Python → Skill #1": a relationship between
   two capabilities that have none. **Only visible in a screenshot** — the JSON
   was correct. Now one continuous fan centred on the agent, pinned by
   `test_no_two_capabilities_share_a_position`.
3. **`Skill #1` / `KB #3` instead of real names.** The sidebar three inches away
   showed "Python Code Review Checklist" while the canvas showed "Skill #1". Two
   labels for one thing is how a user stops trusting both. Now resolved from the
   DB, scoped to the agent's owner, with a `(missing)` suffix when the attached
   row is gone rather than dropping it — a dangling attachment is a broken
   configuration worth showing.

**Verified working end to end:** routes resolve on the live server (401 with a
404 control); the capability map returns real data including the `shell` grant;
a seeded three-call/two-turn run projects over HTTP with the correct fan-out and
fan-in and no self-loops or dangling edges; the page renders with the sidebar
collapsed to one "Automations" entry, the run log docked at the bottom, and the
capability panel listing all seven tools with `Shell` flagged **not served**.

Scratch execution rows were deleted afterwards; the dev database is back to one
agent, two workflows, zero agent executions.

**Two console messages, both pre-existing and neither introduced here:** the
`["hitl","pending"]` query returning `undefined` (Sidebar's poll), and a React
Flow `nodeTypes` warning — both `AgentCanvas` and `WorkflowEditor` define theirs
at module scope, so it is a StrictMode double-mount artifact, not a real defect.

**Still not covered:** a real agent run against a live model. Everything above
uses seeded rows, so the projection is proven but `agent_stream.py`'s
translation has not been watched against an actual streaming turn.
