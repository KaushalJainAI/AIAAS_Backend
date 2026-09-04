# Unifying workflows, agents, and the orchestrator

> ** Deprecated — design-only, never built.**
> The orchestrator / goal tier this doc designs was explicitly cut from the
> ship plan ([AGENT_BLOCKS_PLAN.md](AGENT_BLOCKS_PLAN.md) §6) and remains
> unimplemented as of 2026-08-18. Kept as reference in case the tier is ever
> built again; nothing in it describes the running system.

Written 2026-08-14, revised same day after the orchestrator requirement landed.
Successor to `AGENT_TEMPLATES.md` §8.

> **This doc is the architecture.** The build order for the merge itself — one
> canvas for workflows and agent runs, the sidebar as the agent configurator —
> is `AGENT_WORKFLOW_MERGE_PLAN.md`, and it ships **without** the goal tier
> described here. Start there. This doc is where it grows to.

## 1. The three tiers

```
Orchestrator   owns the goal, the context, and the log.        "Get this done."
    |          Picks specialists, hands context between them,
    |          watches for a stuck agent, decides when done.
    v
Agent          a specialist. Narrow brief, narrow grants.      "Do this part."
    |          Reasons, calls tools, may pause for approval.
    v
Workflow       deterministic hands. Fixed, testable.           "Do exactly this."
```

`AGENT_TEMPLATES.md` §1 already called for "DAGs get demoted — the agent is the
brain, the workflow is the hands." This adds the tier above: **agents get
demoted too.** An agent is no longer the thing you delegate a goal to; it is a
specialist the orchestrator delegates a *sub-goal* to. The orchestrator is what
you delegate the goal to.

### Orchestrator responsibilities (the spec)

1. **Own the end goal** — decompose it, decide when it is met, decide when it is
   unreachable and stop.
2. **Own the context** — one store per goal. Agents do not talk to each other;
   they read from and write to the orchestrator's context.
3. **Hand context between agents** — decide what a receiving specialist needs,
   which is a *narrowing* decision, not a copy.
4. **Supervise** — detect a stuck, looping, or off-brief agent and intervene.
5. **Own the log** — the audit trail across the whole goal, not per agent run.
6. **Absorb a re-steer** — when the user changes the plan mid-run, pause the
   specialist, acknowledge the change, and continue under the new direction.
7. ~~Hold company-wide context~~ — **deferred** (§3). The tier that outlives a
   goal is postponed; everything below is per-goal.

---

## 2. What v1 of this plan got wrong

Recording these because each was a real design error, not a detail.

1. **The graph projection was centred on one agent.** I planned a "capability
   map" of one agent and its tools. With an orchestrator running specialists,
   the view that carries the most information is **agents as nodes, context
   handoffs as edges**. Fixed in §4: three graph levels, not one.
2. **I flattened a hierarchy that is inherently nested.** v1 put one
   `ExecutionLog` per agent run and one `NodeExecutionLog` per tool call — with
   nothing above it. A goal spans many agent runs. `ExecutionLog` **already has
   `parent_execution`, `nesting_depth`, `is_subworkflow_execution`** and v1
   ignored all three.
3. **Per-agent spend caps multiply — this is a safety bug.**
   `check_guardrails` reads `agent.guardrails.spendCapRupees` and compares it to
   *that agent's* monthly spend (`agent_runtime.py:328`). An orchestrator
   fanning out to five agents with ₹500 caps can spend ₹2500 with nothing
   watching the total. A goal-level budget must exist and debit down.
4. **Context handoff had no home at all.** v1 never said where inter-agent
   context lives, what shape it has, or who trims it. That is the core of the
   requirement and the plan was silent.
5. **The WebSocket fan-out assumed a flat execution.** The client subscribes to
   one `execution_{id}`; under nesting the events originate in *child*
   executions and would never reach it.
6. **"The canvas is read-only in topology" was wrong for the top tier.** An
   agent's trace is emitted and must stay read-only — but *which specialists are
   on the team, and the handoff policy between them* is exactly the thing a user
   should author. Three modes, not two.
7. **A detached task is not durable orchestration.** v1 moved execution to
   `spawn()`. Fine for one agent run; wrong for a goal that spans many runs,
   approvals, and possibly days on a maintenance trigger. Goal state must live
   in the DB and be resumable after a restart.
8. **`run_workflow` was the wrong primitive to lead with.** `delegate_to_agent`
   is the primary edge; calling a workflow is the leaf case.

## 3. What exists, and what is dead

**Reusable as-is:**
- `Workflow.kind` + `tool_grants` / `guardrails` / `sandbox` / `agent_context`
  — the agent/workflow merge is already done at the data layer.
- `ExecutionLog.parent_execution` / `nesting_depth` — the nesting spine.
- `NodeExecutionLog` — already shaped like a trace step (`node_id`, `status`,
  `execution_order`, `input_data`, `output_data`, `duration_ms`).
- `RuntimeContext` (`agents/context/execution.py`) — **already goal-oriented**:
  `goal`, `goal_conditions`, `should_continue_for_goal()`,
  `check_goal_condition()`, `record_hitl_decision()`. This is the seed of the
  orchestrator's goal tracking, not something to write fresh.
- The canvas's live execution rendering (`ExecutionOverlay`, `GenericNode`) and
  the `ws/execution/{id}/` consumer.
- `run_agent`'s sink — `AGENT_TRACE` and `ASK_PERMISSION` already emitted.

**Exists but does not fit:**
- `OrchestratorInterface` (`agents/supervision/interface.py`) is **node-level** —
  `before_node` / `after_node` / `on_error`. It supervises a DAG's nodes. It
  cannot express "this agent is stuck" or "hand this context to that
  specialist". Needs a **sibling** interface, not a stretched one.
- `KingOrchestrator` (`executor/king.py`, 1570 lines after design-time authoring
  moved to `executor/generation.py`) supervises one workflow
  execution. The new tier sits *above* it. **Naming collision is a real risk** —
  see §7.

**Dead knobs — confirmed by grep:**
- **`useOrgContext` does nothing.** It is validated, stored, and round-tripped
  through `agents.py`, but `agent_runtime.py` reads only `useEnvironment`
  (line 289). Company context is a switch wired to nothing.
- **There is no organisation.** No `Organization` / `Team` / `Workspace` model
  anywhere in the backend; `core/models.py` has `UserProfile` only.
- **`KnowledgeBase` is user-scoped only** — a `user` FK, no tier or scope
  field. CLAUDE.md describes "file → user → platform tiers"; the model cannot
  express an org tier today.

**Decision (2026-08-14): company-wide context is out of scope entirely, for
now.** No `Organization` model, and no cross-goal context store either — the
durable "company knowledge" tier is deliberately postponed, not smuggled in
under another name. The dead `useOrgContext` knob is deleted rather than left
lying. `ContextEntry.scope` still ships (§4.2) so the tier is a new enum value
later, not a migration of every row.

---

## 4. Design

### 4.1 Data model

```
Goal                     (new) the unit of delegation. Durable, resumable.
 |- goal, status, budget_total/_spent, root_execution
 |- GoalStep             (new) one delegation: agent, sub-goal, status,
 |                             origin, pinned, position     (see §4.6)
 \- ContextEntry         (new) the handoff store — append-only, typed,
                               attributable, scoped         (see §4.2)
Workflow(kind=)          (change) 'workflow' | 'agent' | 'orchestration'
Goal.pending_resteer     (new) a user edit awaiting the next tool boundary (§4.7)
```

Deliberately absent: `Organization`, and any change to `KnowledgeBase`. Both
belong to the deferred company-context tier (§3).

`ContextEntry` is append-only on purpose: a handoff that overwrites is a handoff
you cannot audit. Fields: `goal` (required — every entry belongs to exactly one
goal while the cross-goal tier is deferred), `produced_by_step`, `kind` (fact /
artifact / decision / error), `key`, `value`, `scope`, `tokens`.

### 4.2 Context handoff

The orchestrator never bulk-copies one agent's transcript into the next. On
delegation it **selects**: query `ContextEntry` for the goal, rank against the
receiving agent's brief, and pass a bounded set. This is the same narrowing
`inference/` already does for RAG, applied to run context — which is why it
should reuse the retrieval path rather than grow a second one.

Three reasons this must be selection, not copy: an unbounded transcript blows
the receiving model's window; a specialist given everything stops being a
specialist; and a copy makes the audit trail useless.

**`ContextEntry.scope` — two values now, more later.** Everything in scope for
this work lives and dies with a goal:

| scope | lifetime | who reads it |
|---|---|---|
| `step` | one delegation | the receiving specialist only |
| `goal` | one goal | any specialist on that goal |

Deferred (do **not** build yet): a `user` scope with `goal=NULL` for durable
cross-goal facts, and an `org` scope above it. The column exists from day one so
adding them is an enum change, but nothing reads or writes them and no UI offers
them. A half-built company-context store is worse than none — it would look like
memory and behave like a leak.

### 4.3 Supervision

**Decision (2026-08-14): the orchestrator alone decides fit.** A specialist
never refuses a sub-goal as outside its brief. This removes a round trip and the
failure mode where nobody accepts the work — but it moves the whole cost of a
bad assignment onto supervision, because a mis-assigned specialist will not
object, it will flail. **The supervisor is therefore load-bearing, not a
nice-to-have**, and must ship in Phase 1 with the delegation it protects.

New `AgentSupervisor` sibling to `OrchestratorInterface`, at the *run* level:
`on_step_start`, `on_tool_result`, `on_step_stall`, `on_step_complete`.
Detects: repeated identical tool calls, iteration budget nearing its limit, no
`ContextEntry` produced after N tools, an agent working outside its brief.
Decisions reuse the existing `ContinueDecision` / `RetryDecision` /
`AbortDecision` / `PauseDecision` types.

The specific signal that matters here: **a step producing no `ContextEntry` is
the cheap detector for a bad assignment.** A specialist given work it cannot do
burns iterations without producing facts, and that is observable without a
second LLM judging every step.

### 4.4 Budget

`Goal.budget_total` is set at delegation and debited as each step completes.
`check_guardrails` grows a goal-aware path: an agent run inside a goal checks
the **goal's** remaining budget as well as its own cap, and the lower of the two
binds. Fixes §2.3.

### 4.5 The three canvas modes

| Mode | Topology | Nodes | Edges |
|---|---|---|---|
| **Orchestration** | **authorable** | specialists on the team | handoff policy |
| **Agent run** | read-only (emitted) | tool calls | order + provenance |
| **Workflow** | authorable (unchanged) | node types | data flow |

Orchestration mode is where the sidebar earns its keep: the shelf lists
*agents*, and dropping one adds a specialist to the team.

### 4.6 The plan is a proposal, not a black box

**Decision (2026-08-14): LLM-first, user-editable.** The orchestrator proposes
the team and the sequence; the canvas renders the proposal; the user may edit,
reorder, add, remove, or pin steps before *and during* the run.

This is the pattern `AgentBuilder` already uses — the agent moves the knobs, you
can override every one, and what it touched is visibly flagged. Reuse that
vocabulary rather than inventing a second one: the `Knob` / `touched` treatment
in `AgentBuilder.tsx` becomes proposed-vs-pinned on the canvas.

`GoalStep` carries the provenance that makes this safe:

- `origin` — `proposed` (LLM) | `edited` (LLM's step, user changed it) |
  `added` (user's own step)
- `pinned` — **replanning must not clobber user intent.** When the orchestrator
  re-plans after a failure, pinned and `added` steps survive; only unpinned
  `proposed` steps may be rewritten.

**Completed steps are immutable.** Rewriting the past would make the log a lie,
and the log is the orchestrator's job. The API enforces this server-side; the
canvas greys them out, but greying out a control is not enforcement.

### 4.7 Re-steer: the user changes the plan mid-run

**Decision (2026-08-14):** editing a running plan is supported, and the
in-flight agent **pauses, acknowledges the re-steer, then continues under the
new direction.** It does not silently absorb the change and it does not get
killed and restarted.

This is a *different mechanism* from the HITL approval pause, and conflating
them would be a design error — the direction of initiation is opposite:

| | initiated by | agent's move | resumes when |
|---|---|---|---|
| **HITL approval** | the agent | asks, waits | user answers |
| **Re-steer** | the user | acknowledges, re-plans | immediately, on its own |

So a running step has three states, not two: `pending` (freely editable),
`running` (interruptible — see below), `completed` (immutable).

**The protocol.**

1. The edit is **recorded immediately** on `Goal.pending_resteer`, never applied
   inline. The user's intent must survive even if the step crashes a moment
   later.
2. The runtime applies it **at the next safe boundary — between tool calls.** A
   tool call already in flight runs to completion. An HTTP POST that has fired
   has already had its side effect; "cancelling" it would only hide that.
3. The agent **acknowledges**: it is shown the diff (what changed, what was
   added or dropped) and states in one line how it will proceed differently.
4. The acknowledgement is persisted as a `ContextEntry` of kind `decision`, not
   just streamed as a toast. Without it the trace shows an inexplicable pivot
   two steps later, and the log stops explaining itself.
5. The orchestrator then re-plans around the edit — **the user's edit is an
   input to re-planning, not a veto on it.** Pinned and user-added steps still
   survive per §4.6; what the user changed is treated as newly pinned.

**Mechanism — reuse, don't fork.** `chat/turn/agent.py::_require_approval` already
pauses a run with LangGraph's `interrupt()` and resumes it by `thread_id`
through the checkpointer. Re-steer rides the same machinery with a different
reason and no waiting-on-a-human step. A second pause mechanism would mean two
things that must agree about what "paused" means, which is the trap
`AGENT_TEMPLATES.md` §8 already recorded when it chose to borrow the chat loop
rather than fork it.

**New sink event:** `RESTEER`, alongside `ASK_PERMISSION`. The canvas needs to
distinguish "waiting for you" from "absorbing what you just told me" — they look
identical otherwise, and only one of them is the user's turn to act.

---

## 5. Phases

**Phase 0 — clear the dead wood (blocking, small).**
Delete `useOrgContext` end to end — serializer (`agents.py:110`), `to_config` /
`apply` round-trip, `AgentConfig` type, the `AgentBuilder` control, and
`tests/test_agents.py:62` which currently asserts on it. Settle the naming collision
in §7. No org model, no `KnowledgeBase.scope` — both belong to the deferred
company-context work and neither is needed by anything below.

Deleting a knob users may have set is a migration, not just a code change: drop
the key from stored `agent_context` blobs in the same migration so nothing reads
a field that no longer exists.

**Phase 1 — the goal tier.**
`Goal` / `GoalStep` / `ContextEntry` models; `agents/goal_runtime.py`
(propose plan → delegate → select context → supervise → judge done), durable and
resumable; goal-level budget in `check_guardrails`; `delegate_to_agent` and
`run_workflow` as granted tools, both closed by default and listed in
`requirements`.

Plan mutation is part of this phase, not a later polish — `origin` / `pinned`
semantics (§4.6), the completed-steps-are-immutable rule enforced in the API,
replanning that preserves pinned and user-added steps, and the **full re-steer
protocol of §4.7**: `Goal.pending_resteer`, application at the tool boundary,
the persisted acknowledgement, and the `RESTEER` sink event.

The `AgentSupervisor` (§4.3) ships in this phase too, not later. Since the
orchestrator alone decides fit, supervision is the only thing standing between a
bad assignment and a silently flailing specialist.

**Phase 2 — observability spine.**
Nest agent runs under the goal's root `ExecutionLog` via `parent_execution`;
write `NodeExecutionLog` per tool call; **fan events up to the root group** so
one subscription sees the whole tree; bridge `ASK_PERMISSION` to `HITLRequest`
so the canvas approve UI works unchanged.

**Phase 3 — graph projection.**
`agents/agent/graph_projection.py` with three projections (orchestration /
agent-run / workflow) behind one response shape, so the frontend has one
renderer. Endpoints + `docs/API.md` rows in the same change.

**Phase 4 — frontend.**
Extract `CanvasShell` from `WorkflowEditor.tsx` (1626 lines) with workflow mode
behaviourally identical — **alone, verified, before anything else**. Then
`useGraphStore` gains `mode` + `topologyEditable` (enforced in the store, not
the UI); `AgentTraceNode` and `SpecialistNode`; `NodePanel` → mode-aware shelf
(agents in orchestration mode, node types in workflow mode); step drill-down
reusing `NodeConfigPanel`'s output viewer; route/list unification with
redirects.

Orchestration mode specifically: proposed-vs-pinned styling borrowed from
`AgentBuilder`'s `Knob` treatment, drag to reorder pending steps, pin/unpin,
and a context inspector showing what each handoff actually carried — the
handoff is the thing users will not believe until they can see it.

## 6. Verification

Backend — `pytest`:
- goal decomposition, resumption after simulated restart, done-judgement
- **context handoff is a narrowing**: receiver gets a bounded, relevant subset —
  assert it is *not* the full transcript
- **budget**: N agents under one goal cannot exceed `Goal.budget_total`
  (direct regression for §2.3)
- supervisor fires on a looping agent
- `delegate_to_agent` / `run_workflow` refused without the grant
- event fan-up: a leaf tool call reaches the root execution group
- **replanning preserves pinned and user-added steps** and rewrites only
  unpinned proposed ones (§4.6)
- **a completed step rejects edits at the API**, not just in the UI
- **re-steer (§4.7)**, its own test group since it is the subtlest thing here:
  an edit landing mid-step is recorded on `Goal.pending_resteer` and survives a
  crash of that step; it applies at the tool boundary and never mid-call; the
  acknowledgement is persisted as a `decision` `ContextEntry`; a `RESTEER` event
  is distinguishable from `ASK_PERMISSION` on the wire; and a re-steer arriving
  while the run is *already* paused for approval resolves in a defined order
- `scope='goal'` context does not leak between goals; no code path writes a
  `user`-scoped entry (the deferred tier stays unbuilt, not half-built)
- Phase 0 regression: no reference to `useOrgContext` survives in backend,
  frontend, or stored `agent_context` blobs
- Known env gotcha: `orchestrator/` failures are often missing-whitenoise, not
  regressions; `nodes/` is the clean signal.

Frontend — `npx tsc -b --force` (plain `tsc --noEmit` checks zero files here),
`npm run lint`, `npm run build`, and an endpoint-wiring check resolving every
new `apiClient` path against the URLconf.

End-to-end: give a goal → orchestration canvas renders the team → run → steps
light up live → a specialist pauses on a sensitive tool → approve from the
canvas → context visibly hands to the next specialist → goal closes → replay
from history.

## 7. Risks

- **`WorkflowEditor` extraction** — 1626 lines with live execution state.
  Isolated, verified first.
- **Naming collision.** The `orchestrator/` Django app is workflow CRUD;
  `KingOrchestrator` supervises one execution; the new tier is a third thing
  called "orchestrator". Decide the vocabulary in Phase 0 and apply it
  everywhere, or every later conversation pays the tax.
- **`Organization` touches auth and every ownership query.** Ship it first,
  alone, with `get_object_or_404(..., user=request.user)` audited — a scope bug
  here is a cross-tenant data leak, not a bug.
- **Orchestrator cost.** A supervising LLM on every step can cost more than the
  work. `SupervisionLevel` already has `ERROR_ONLY` and `NONE` — reuse it.
- **Decomposition quality is the product.** If the orchestrator picks the wrong
  specialists the whole tier is worse than one capable agent. Keep single-agent
  delegation as a first-class path, not a degenerate case.

## 8. Decisions and open questions

**Settled 2026-08-14:**
1. ~~Is "organisation" real multi-tenancy?~~ **Out of scope for now.** No org
   model and no cross-goal context store; the `useOrgContext` knob is deleted.
   `ContextEntry.scope` ships with `step` and `goal` only (§4.2).
2. ~~LLM decomposition or user-authored plan?~~ **Both, LLM-first.** The
   orchestrator proposes, the canvas renders the proposal, the user edits and
   pins; completed steps are immutable (§4.6).
3. ~~Can a specialist refuse a sub-goal?~~ **No — the orchestrator alone decides
   fit.** Consequence: supervision becomes load-bearing and ships in Phase 1
   (§4.3).
4. ~~Is an edited plan final, or does the orchestrator re-plan around it?~~
   **It re-plans.** The agent pauses, acknowledges the re-steer, and continues
   under the new direction; the edit is an input to re-planning, not a veto on
   it (§4.7).

**Still open:**
5. Does a goal that exhausts its budget mid-run pause for a top-up, or fail?
   Pausing is friendlier; failing is the behaviour the spend cap already has
   (`AgentRunRefused`), and two different answers to "out of money" would be
   worse than either.
6. Falls out of §4.7: if a re-steer arrives while the run is paused for HITL
   approval, which resolves first? Needs one defined order, not whichever the
   scheduler happens to reach.
