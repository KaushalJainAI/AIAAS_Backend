# Coding Agents Plan — a coding roster, a lead that can steer it, file leases, and a live plan panel

**Status:** built 2026-09-22 (C1 → C2 → C4 → C3 → C6 → C5 → C7, in that order).
Depends on P6 (`shell` tools, `workspaces/`) and P7 (missions' `wait_for` pattern) from
`PLATFORM_CAPABILITIES_PLAN.md`, both already in the working tree.

**Goal.** Explore (`/templates`) offers a *team* of coding subagents. Each one
differs in its grants, tools, guardrails and playbook. A **Coding lead** can
sequence them, run the ones that don't conflict in parallel, and interrupt,
steer or stop any of them mid-task. Any file an agent is writing is **leased**,
so no other agent can write it at the same time. When a file changes under an
agent that has read it, that agent is **told**. The user watches the lead's plan
and every worker's state in a **side panel**, as in OpenCode, and can tighten
permissions per worker.

---

## 1. Where we are (verified 2026-09-22)

| Piece | State | Where |
|---|---|---|
| Workspace code tools | Built: `ws_list/read/search/write/edit/apply_patch/run`, `git_*`, `open_pull_request` | `chat/tools/code.py`, grant `shell` |
| Change record | Every write creates a `CodeChange(run, path, before_hash, after_hash, diff)` | `workspaces/models.py`, `code.py::_record_change` |
| Coding templates | Only two: `repo-assistant` (ask, shell+fileOps) and `reviewer` (plan, `findings` contract); pack `code` installs both | `agents/gallery.py` |
| Per-agent narrowing | Grant → `toolScope` (which tools) → `toolPermissions` (allow/ask/deny per tool) → autonomy ladder | `agents/agent/runtime.py` |
| Delegation | `invoke_subagent` → `run_fanout`, which **awaits** `asyncio.gather` over at most 8 workers; depth, budget and result-size bounds | `agents/agent/orchestrator.py` |
| Steering / cancel | Only per *agent* (`agents/{id}/steer/`, `run_cancel`); a lead has no tool to steer or stop a worker it started | `agents/views/runs.py` |
| Todos | `update_todos` → `meta['todos']`; rendered **inline in the transcript** (`StandaloneChat.tsx:2225,2475`, `Runs.tsx:343`), not as a side panel. A worker's todos never reach the parent | `chat/turn/todos.py`, `TodoPanel.tsx` |
| Locks | **None.** Two workers given the same project can `ws_edit` the same file concurrently; the later edit wins without an error | — |
| Skills | `skills.Skill` rows attached by id through `agent_context['skills']`. A template cannot carry them, because templates travel without ids | `skills/models.py`, `runtime.py:1000` |
| `/code` page | Not built | P6 §9.2 |

The gap is not tools. The gap is **roles, coordination and visibility**:
1. Nothing makes a scout different from an implementer.
2. A lead cannot act while its workers run, because the fan-out tool call
   blocks until all of them finish.
3. Nothing stops two writers colliding.
4. The user sees the plan only by scrolling the transcript.

---

## 2. Rules this plan follows

1. **Configuration, not code paths.** Every coding agent is a gallery template
   (a `SubAgent` config). No `kind` column and no special "coder" runtime. The
   lead is an agent holding `subAgents`, as today.
2. **The model proposes, code enforces.** The lead decides the order of work.
   Leases, stale-write checks, most-restrictive-wins permissions and budget
   division are enforced in dispatch code, never by prompt.
3. **Refuse with a reason, never drop silently.** A conflicting write, a
   stale edit or an overlapping claim comes back as a sentence that names the
   holder and the fix. A model told only "denied" retries until it hits the
   iteration cap.
4. **Correctness comes from hashes; notices are a courtesy.** An agent may miss
   a "this file changed" notice. It cannot miss a stale-write refusal.
5. **Never widen.** A worker never holds more than its template allows or more
   than its parent holds. `invoke_subagent` already applies most-restrictive-wins
   to `toolPermissions`; the new axes below follow the same rule.
6. **The 913 MB box.** Workers run in-process and the workspace runs in the
   remote engine. The default parallelism for code is **3**, not 8.

---

## 3. C1 — The coding roster

Seven templates in pack `code`. The two that exist are kept. `repo-assistant`
stays the single-agent choice for someone who does not want a team.

| Slug | Job | Grants | Tool scope (narrowed) | Autonomy | Writes (`writePaths`) | Commands (`commandScope`) | Contract | Model tier |
|---|---|---|---|---|---|---|---|---|
| `code-scout` | Map the repo: where things live, entry points, conventions, test commands | `shell` | `ws_list/read/search`, `git_status/diff` | `plan` | none | none | `findings` (kind=`map`) | cheap/fast |
| `code-architect` | Turn a goal plus the scout's map into a **task plan** with file claims and dependencies | `shell`, `fileOps` | read tools only | `plan` | none | none | **`code_plan`** (new) | strongest |
| `code-implementer` | Make one task's change and run the relevant tests | `shell` | read + `ws_edit/apply_patch/write`, `ws_run` | `auto` (edits), `ask` for anything else | the task's claims only | `test`, `lint`, `build` | **`patch`** (new) | mid |
| `code-test-writer` | Write or extend tests for a task; never edits source | `shell` | read + `ws_write/edit`, `ws_run` | `auto` | `**/test*/**`, `**/*.test.*`, `**/*_test.*`, `**/tests/**` | `test` | `patch` | mid |
| `code-debugger` | Reproduce a failure, bisect, fix the smallest thing | `shell` | read + edit + `ws_run` | `ask` | the task's claims | `test`, `build`, `run` | `patch` | strongest |
| `reviewer` *(exists)* | Read-only review of the combined diff | `shell`, `fileOps` | read tools | `plan` | none | `test`, `lint` (read-only run) | `findings` | strongest |
| `code-integrator` | Commit, push the branch, open the PR; the **only** role holding `git_commit/push/open_pull_request` | `shell` | `git_*`, `open_pull_request`, `ws_run` | `ask`, with push and PR always `sensitive` | none | `test` (final gate) | `files` + PR url | cheap |
| `coding-lead` | The orchestrator: plans with the architect, dispatches, watches, steers, integrates | `subAgents`, `shell` (read only) | read tools + the §6 dispatch tools | `ask` | none | none | `patch` summary | strongest |

`coding-lead` gets `delegatesTo` set to the other six at install time. That uses
the existing delegation scope, so a lead cannot delegate outside the roster.

### 3.1 Two new axes, following the existing grant/scope pattern

Tool-level narrowing (`toolScope`, `toolPermissions`) cannot tell "may edit
`src/api/`" from "may edit anything". Coding agents need two finer scopes.
Both are stored in `agent_context` and validated in `AgentSerializer`. Both use
**empty = unrestricted**, the default every scope here takes because agents
predate them.

- **`writePaths`**: glob list relative to the project root. Checked in
  `ws_write/edit/apply_patch` *before* the lease is taken (the order matters; see
  C2). The path is normalised first, with `..` clamped as `vfs` does. At runtime
  it is further intersected with the task's **claims**, so an implementer can
  write only what its task claimed, even if its template allows more.
- **`commandScope`**: a list of command classes (`test | lint | build | run |
  install | any`). The project's actual commands are resolved from the scout's
  map, or from a `CodeProject.commands` JSON field (new), for example
  `{"test": "pytest -q", "lint": "ruff check ."}`. `ws_run` accepts either
  a class name or a literal command. A literal must match one of the resolved
  commands for an allowed class (prefix match on argv, not a substring), or the
  class must be `any`. This replaces the current `'.ssh' in lowered` denylist
  as the *policy*. The workspace VM is still the *boundary*.

Parent → worker: `writePaths` and `commandScope` are intersected
(most-restrictive-wins) in `invoke_subagent`, next to the `toolPermissions`
merge.

### 3.2 Skills (playbooks) that travel with a template

Templates cannot carry `Skill` ids. Coding playbooks therefore ship **as code**:
`agents/playbooks/code/*.md`, loaded by slug. The config gains
`playbooks: ['python-testing', 'small-diffs', ...]`, and the prompt builder
appends them after the brief, in the session-stable system prompt. That is
allowed there because they are static. A user's own `Skill` rows still attach
by id in the builder as today. Initial playbooks:
- `small-diffs`
- `run-tests-before-claiming-done`
- `read-before-edit` (explains the stale-write rule)
- `python-testing`
- `ts-react`
- `git-hygiene` (integrator only)

### 3.3 New contracts (`agents/contracts.py`)

- **`code_plan`**: `{goal, tasks: [{id, title, agent, instructions, claims:
  [glob], reads: [glob], depends_on: [id], acceptance}], risks}`. Repair
  function: coerce a single task to a list, drop self-dependencies, refuse
  cycles with the cycle named. `claims` is required and non-empty for any
  task whose agent can write.
- **`patch`**: `{summary, changes: [{path, kind, change_id}], tests: {command,
  passed, output_tail}, followups}`. `change_id` points at `CodeChange`, so the
  lead and the UI link to the real diff rather than to one restated in prose.

---

## 4. C2 — File leases: writing takes a lock

### 4.1 Model

```
CodeLease(project FK, pattern CharField,          # normalised path or dir/**
          holder FK ExecutionLog, holder_label,   # "Implementer #2"
          mode = write,                           # reads never lock
          task_id, acquired_at, heartbeat_at, expires_at)
unique-ish: enforced in code under select_for_update on the project row
```

**Only writes lock.** Readers are never blocked. A reader learns about
concurrent writes through C3 instead. Blocking reads would serialise the scouts
and reviewer for nothing.

### 4.2 When a lease is taken

1. **At dispatch (planned).** When the lead starts a task, its `claims` are
   leased atomically before the worker's run opens. If any claim overlaps a live
   lease, **the task does not start**. The lead gets
   `"claims src/api/** overlap Implementer #1's lease on src/api/client.ts
   (task t3); wait for t3 or re-scope"`. Overlap is computed on glob prefixes:
   `a/**` overlaps `a/b.ts`, and two exact paths overlap only if equal.
2. **At write (lazy).** `ws_write/edit/apply_patch` check that the path is
   covered by a lease *this run* holds. If it is uncovered and no one else holds
   it, the lease is taken implicitly. This covers a single agent with no lead,
   and a file the plan missed. If someone else holds it, the write is refused
   with the holder's name.
3. **The integrator** takes a project-wide `.git/**` lease for commit/push, so
   nothing writes while a commit is being assembled.

### 4.3 When a lease is released

- On every terminal path of the holder run (completed, failed, cancelled), in
  the same place `forget_thread` is called. **Not** on `paused`: a run waiting
  on approval still owns its half-made change.
- **Expiry**: `expires_at = heartbeat + 10 min`, with the heartbeat renewed on
  each tool call by the holder. `agents/recovery.py` already sweeps dead runs
  and also clears their leases, so a crashed worker cannot hold `src/**` forever.
- **Manually from the panel** ("Release lock"). Releasing does not revert the
  change. It only lets others write.

### 4.4 Stale-write guard (the actual correctness guarantee)

`ws_read` returns the file's `sha256`. The run records it in a per-run read set
(`meta['reads'][path] = hash`, which lives in graph metadata, survives curation,
and is cheap). `ws_edit` and `ws_apply_patch` compare the current hash with the
last one *this run read*. If they differ, the edit is refused: `"src/x.ts
changed since you read it (by Implementer #1 / by you in the editor); re-read
it"`. This catches changes from other agents, from the user in the editor, and
from a `git checkout`. `ws_write` of a *new* file checks for existence instead.

---

## 5. C3 — Change awareness: who needs to know

1. **The bus.** `_record_change` already writes `CodeChange`. It also publishes
   `{project, path, change_id, by_run, by_label}` to a channels group
   `code:<project_id>`. It is the same event the UI consumes, so there is no
   second copy of it.
2. **Who is affected.** A small matcher (`workspaces/awareness.py`) finds every
   *live* run on the project, other than the writer, whose read set contains the
   path or whose claims/`reads` globs cover it.
3. **Delivery** reuses the steering mailbox, with a new entry kind:
   `steering.post(thread, text, kind='notice')`. A notice is drained on the same
   `tools → steering → agent` edge, but it renders as a **`system` context
   message**, not a `HumanMessage`. It is information, not an instruction from
   the user. Notices are coalesced per path (three edits to one file in a
   batch → one line) and capped by `MAX_BATCH_CHARS` like steers.
4. **The lead is always told**, as a compact event rather than a notice (see §6
   `wait_tasks`). The lead may choose to forward it (for example, re-steer a
   test-writer whose target just changed shape).
5. **The user is told** through the side panel (§7): the file flashes with the
   writer's label. A change the *user* makes in the editor produces a notice to
   every agent that read the file, with `by_label = "you"`.

---

## 6. C4 — The lead can start, watch, steer and stop workers

Today `invoke_subagent` blocks inside one tool call until every worker ends, so
the lead has no turn in which to react. Coding needs **detached** workers and a
wait that returns on *events*, the pattern P7's `wait_for` already uses for
missions.

New tools, available only with `subAgents`, in `chat/tools/agents.py`:

| Tool | effect | Behaviour |
|---|---|---|
| `start_tasks(tasks)` | reversible | Takes `code_plan` task objects. For each ready task (dependencies done): checks that its claims don't overlap each other or live leases, acquires leases, then starts a worker run detached through `spawn` with the task's `agent`, `instructions` as the goal, the plan's goal as `briefing`, and `writePaths ∩ claims`. Returns handles immediately. Refuses a task whose dependencies are unfinished, naming them. Parallelism is capped by `MAX_CODE_WORKERS=3` and budget is split with `divide_budget` |
| `wait_tasks(handles?, until='any'\|'all', timeout_s≤300)` | read | Returns on the first **event**: a task finished, failed, paused for approval, blocked (a todo marked `blocked`), a lease conflict, or a file change touching another task's reads. Also returns on timeout, reporting progress. Each event is one line, and the full result goes through `bound_results`/spill as today |
| `task_status(handle?)` | read | A snapshot: status, the worker's current todo, last step, leases held, spend |
| `steer_task(handle, message)` | reversible | Posts into the worker's mailbox (its thread id). Same queue and caps as a user steer |
| `stop_task(handle, reason)` | reversible | Calls `cancel_agent_run` on the worker. Leases are released and the worker's `CodeChange`s stay for the lead to keep or revert |
| `revert_task(handle)` | reversible, `sensitive` | Reverts that worker's `CodeChange`s in reverse order, refusing any file changed since by someone else |

`invoke_subagent` stays unchanged for research-style fan-outs, where blocking is
fine.

**Sequencing is the lead's; safety is the code's.** The lead's brief says:
1. Scout.
2. Architect → `code_plan`.
3. Mirror the plan into `update_todos`, one todo per task, with `owner` and
   `task_id`.
4. `start_tasks` on everything ready.
5. Loop `wait_tasks`, updating todos and starting newly ready tasks.
6. Once all tasks are done, run the reviewer on the combined diff, feed its
   findings back as new tasks or stop.
7. The integrator, only after the user approves the diff.

The code guarantees that no two tasks write the same file whatever order the
lead picks. A lead that tries two overlapping tasks together gets the refusal
and must sequence them.

**Workers are not leads.** Workers keep the narrowed toolbox (no `subAgents`),
so depth stays 1 for code. A debugger that wants help says so in its `patch`
`followups`, and the lead decides.

---

## 7. C5 — The user controls permissions

- **Per-worker permissions at install.** The `code` pack install screen shows
  the roster as a matrix: rows are agents, columns are
  `writePaths / commandScope / autonomy / key tool permissions`, and every cell
  is editable before the rows are written. These are the same keys the builder
  saves, through `AgentSerializer`, so there is only one copy.
- **Per-run tightening.** From the side panel, the user can switch any live
  worker's autonomy (the existing mid-run switch, now addressable per
  worker handle) and pause or stop it. Changes are not retroactive, as today.
- **Approvals name the worker.** HITL rows opened by a worker carry
  `holder_label` and the task title, so the Inbox reads "Implementer #2 (task t3:
  add retry to client) wants to run `npm install axios-retry`" rather than a
  bare tool call. `describe_call` gets that label passed in; it does no lookup
  of its own.
- **Never widen.** A worker's effective `writePaths`, `commandScope` and
  `toolPermissions` are the intersection of its template, the lead's, and the
  task's claims.

---

## 8. C6 — The plan panel (OpenCode-style side panel)

### 8.1 What streams

A worker's stream never reaches the parent today. Add parent-directed frames:
the worker's runtime knows its `parent_step` → parent execution → parent thread,
and publishes to it:

| Frame | Payload |
|---|---|
| `task_update` | `{task_id, handle, label, agent_slug, status, current_todo, last_step, spend}`, throttled to at most 1 per second per task |
| `lease_update` | `{project, leases: [{pattern, holder_label, task_id}]}` |
| `code_change` | `{path, change_id, by_label, lines_added, lines_removed}` |

`output_data` gains `tasks` (the final state of each task), as it gained
`todos` and `charts`, so `/runs` can redraw the panel for a finished run.

The todo item schema gains optional `owner` and `task_id`. This is backward
compatible: `normalise` keeps unknown-free items as they are.

### 8.2 The component

`components/orchestration/PlanPanel.tsx` is a **right-side, sticky** panel on
chat and `/runs`, and on `/code` when that page lands:

1. **Plan**: the lead's todos in order. Each shows its status glyph, owner chip
   and elapsed time, and the current item is highlighted. It is always visible
   and does not scroll with the transcript.
2. **Workers**: one lane per task handle. Each lane shows the agent icon and
   label, status, current todo, last tool step (via `describe_call` text),
   spend, and actions (**Steer**, **Pause/autonomy**, **Stop**, **Open run**).
3. **Locks & changes**: the files currently leased, with a lock icon and holder,
   and recent changes that flash on arrival. A click opens the file card or
   diff (`FilePreview`/`CodeChange` diff).

The inline `TodoPanel` in the transcript stays for chats with no delegation.
When a run has tasks, the transcript shows a one-line "Plan updated (4/9)" pill
that focuses the panel instead of repeating the list.

**Mobile**: the panel collapses to a bottom sheet with a progress pill (`4/9 ·
2 running`), following the mobile layout contract (the shell is
overflow-hidden, so the panel owns its own scroller).

It follows the "a ticking clock belongs in its own component" rule. Elapsed
timers live in a leaf component so worker frames don't re-render the
transcript. `PlanPanel` is `memo`'d and fed from a reducer (`usePlanStream`)
beside `useChatStream`.

---

## 9. C7 — Evaluation

Guardrail suite `guard-code-coordination` (100% bar, because it tests our code):
1. Two tasks claiming the same file in one `start_tasks` → the second is
   refused with the holder named; the first runs.
2. A worker edits a file outside its claims → refused, nothing written.
3. A stale edit after another worker's change → refused; the model re-reads and
   succeeds.
4. A worker killed mid-run → leases released by the recovery sweep within TTL.
5. A change to a file a live reviewer read → the notice is delivered at its next
   boundary, exactly once, coalesced.
6. `commandScope=['test']` + `ws_run('curl …')` → refused.
7. A lead cannot grant a worker `writePaths` wider than its own.

Built 2026-09-22: cases 1–7 are pinned deterministically rather than in paid
model runs — `agents/tests/test_code_dispatch.py` (1, 7, plus the
lead-plus-two-stub-workers frame test), `workspaces/tests/test_stale_write.py`
(2, 3, 6), `workspaces/tests/test_leases.py` (1 at the lease level),
`workspaces/tests/test_awareness.py` + `chat/tests/test_steering.py::NoticeTests`
(5), `agents/tests/test_recovery.py::RecoveryOutcomeTests::
test_a_crashed_worker_releases_its_leases` (4),
`agents/tests/test_code_scopes.py` (7 at the scope level). The coordination
refusals need a live workspace engine (`WORKSPACE_ENGINE` is `none` here), so
a paid-model suite would fail on the engine before reaching the enforcement;
the unit files above run in CI at 100% instead. What the benchmark harness
gains now is the grader the team suite needs: `code_changes_within` (every
`CodeChange` inside the task's claims, populated by the runner from the
execution; `eval/tests/test_graders.py::CodeChangesWithinTests`) plus
`code-lead` / `code-implementer` bench agents (`eval/benchmarks/agents.py`).
`work-code-team` itself — fixture repo, three repeats, scored against
`repo-assistant` alone — still needs a workspace engine and the paid-runs
go-ahead before it runs.

Capability suite `work-code-team` (pass@1 and pass^3):
- Fixture repo with three independent bugs in three modules plus one
  cross-cutting change. Graded by running the tests in the workspace **and** by
  checking that each `CodeChange` touches only its task's claims.
- Reported against `repo-assistant` alone on the same cases. If the team is
  not better or faster, the lead is overhead.

Unit tests (new files):
- `workspaces/tests/test_leases.py`
- `workspaces/tests/test_stale_write.py`
- `workspaces/tests/test_awareness.py`
- `agents/tests/test_code_dispatch.py`
- `agents/tests/test_code_scopes.py`
- `chat/tests/test_steering.py::NoticeTests`
- `src/lib/__tests__/planStream.test.ts`

Add one `test_turn_output_e2e`-style test that drives a lead with two stub
workers and asserts on the frames the client receives. The todo panel shipped
invisible once while six suites were green, so a unit test per hop is not
enough.

---

## 10. Phases

| Phase | Ships | Exit criterion |
|---|---|---|
| **C1** Roster | Six new templates, `code_plan` + `patch` contracts, `writePaths`/`commandScope` + serializer validation, `CodeProject.commands`, code playbooks, pack `code` extended | Gallery test passes; every template installs through `AgentSerializer`; a scope wider than the parent's is a 400 |
| **C2** Leases | `CodeLease`, lease at write + at dispatch, stale-write guard, release on terminal paths + recovery sweep | Guardrail cases 1–4, 7 at 100% |
| **C3** Awareness | Channels bus, matcher, `kind='notice'` in the mailbox | Guardrail case 5; notice never renders as a user turn |
| **C4** Dispatch | `start_tasks`, `wait_tasks`, `task_status`, `steer_task`, `stop_task`, `revert_task`; `coding-lead` template | e2e lead + two workers; `stop_task` releases leases and keeps changes |
| **C5** Permissions UX | Pack install matrix, per-worker autonomy switch, labelled approvals | A user can install the pack with a tightened implementer and see it enforced |
| **C6** Panel | `task_update`/`lease_update`/`code_change` frames, `output_data.tasks`, `PlanPanel`, mobile sheet | Playwright: start a team task → plan fills, lanes move, lock icons appear and clear, `/runs` redraws it after the fact |
| **C7** Eval | Both suites, scorecard vs `repo-assistant` | Guardrails 100%; the team beats the single agent on pass^3 or wall time. Otherwise that finding is written down and the lead is not promoted in Explore |

Order: C1 → C2 → C4 → C3 → C6 → C5 → C7. Leases must exist before anything runs
in parallel, so C2 comes before C4. Awareness is only useful once there are
concurrent writers. The panel is where the value becomes visible, so it lands
before permission polish.

**Docs to update in the same changes:** `API.md` (no new routes in C1–C4; C5 adds
the pack matrix to the install payload), `CLAUDE.md` (one paragraph per phase,
in the house style), `AGENT_TEMPLATES.md`, and `PLATFORM_CAPABILITIES_PLAN.md` §9
(cross-reference).

---

## 11. Decisions needed from the owner

Decided 2026-09-22, all five on the recommendation:

1. **Shared tree with leases.** Worktrees would hide live changes from the
   other agents, against the "notify others" requirement. Worktrees stay a
   later opt-in for fan-outs above 3.
2. **Default parallelism: 3** (`MAX_CODE_WORKERS`), for the current box.
3. **Implementer autonomy: `auto`** for edits inside its claims (reversible
   through `revert_task`); `ask` for anything else.
4. **Lead model: the strongest available** — sequencing mistakes are the
   expensive ones — with cheaper workers per the table.
5. **Explore shows the pack card up front, with the individual templates
   under it.** A lone `code-implementer` with no plan to follow is a worse
   `repo-assistant`; the `code` pack lists all nine with the roster beneath.

## 12. Deliberately not doing

- **Read locks.** Readers are never blocked; hashes and notices cover them.
- **A code-driven scheduler that runs the whole plan without the lead.** It
  would be simpler and more predictable, but it could not re-plan on a reviewer
  finding or a failed test. That judgment is the lead's job. The code only
  guarantees safety.
- **Nested leads** (a worker delegating further). Depth stays 1 for code.
- **Automatic merge of conflicting edits.** A conflict is refused and
  sequenced, never merged by us.
- **BrowserOS.** It is parked. The panel ships in `better-n8n-frontend` only.
