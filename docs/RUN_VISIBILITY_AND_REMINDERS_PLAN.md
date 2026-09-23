# Run Visibility + Reminders Plan — the orchestrator sees the platform's jobs, then tells the user about them

Status: **APPROVED — implementing.** Owner direction (2026-09-23): the
notification service needs eyes first — the orchestrator must see all running
jobs for the particular user only, with status and todo progress, so it can
answer about them, notify about them, and take their context when the user
points at one (or when it needs it). Timing stays user-decided throughout.

---

## 1. Requirement (confirmed)

1. The orchestrator (chat or agent run) can list **all jobs for this user
   only** — running first — with **status and todo/task progress**.
2. That visibility powers **answering** ("is my research done?"), **notifying**
   (completion, at-a-time, heartbeat), and **context-taking** (user says "that
   invoice job" → the agent resolves it and reads its goal, progress and
   answer).
3. Reminders fire **at a particular time** (one-shot) or on a **heartbeat**
   (`hourly`/`daily`/`weekly`) the user stated. The agent never invents
   timing.

## 2. What already exists

| Piece | State |
|---|---|
| `notify_user` (immediate ping) | Shipped, `ALWAYS_AVAILABLE`, capped per run |
| `get_agent_run` (one run, by id) | Shipped, but status + answer only — no goal, no progress, no discovery |
| `ExecutionLog` → `AgentTurn` → `AgentStep` | Written live every turn; the recording never depended on a graph view |
| `output_data` (`todos`, `tasks`, `files`, `charts`) | Written **at run close only** — a running run's plan lives in graph state, not on the row |
| Notification delivery (feed row + socket + web push) | Shipped (`notifications/utils.py`, `webpush.py`) |
| Reminder sweep halves | In tree: `ScheduledNotification` model + migration, `scheduled.py`, beat task, `manage.py send_scheduled_notifications`, 60s beat entry |

## 3. The two gaps (and why they are separate)

**Gap 1 — no discovery, no progress.** `get_agent_run` needs an
`execution_id` the caller already holds, and reports status + answer. There
is no "what is running for this user", and no compact progress anywhere:
`logs/queries.py::execution_detail` is the full trace (turns with reasoning,
steps with payloads) — the right shape for `/runs`, the wrong shape for a
model's context window.

**Gap 2 — no future.** `notify_user` fires now. Nothing fires at 9am or every
hour. (Half-built in the tree; finished by this plan.)

## 4. Design rules

1. **User scope is the whole privacy model.** Every read filters
   `user_id`; a foreign `execution_id` answers "No such run for this user"
   — the same ownership-before-status rule `get_agent_run` already keeps.
   UUIDs are unguessable, not access control.
2. **Compact, never the trace.** List rows and progress blocks carry counts,
   short excerpts and capped lists — never full reasoning, never step
   payloads. A tool answer that costs thousands of tokens on every call is a
   tool the model stops calling.
3. **No graph-state peeking.** Live todo text lives in the checkpointer,
   which may belong to another process/saver; the tool reads rows only
   (turns, steps, live code-task buckets best-effort in-process,
   `output_data` once closed) and says which source each signal came from.
   A progress report that is sometimes silently stale is worse than one that
   names its sources.
4. **Read-only joins `ALWAYS_AVAILABLE`.** Listing your own runs and reading
   their progress reaches nothing but your own rows — the same terms as
   `notify_user` — so chat and agent runs both get it with no grant change,
   and `toolScope` narrowing never touches it.
5. **Timing is quoted, not chosen.** The schedule tool takes an exact time
   the user stated; heartbeat means one of `hourly`/`daily`/`weekly`. The
   description forbids inventing reminders, and completion uses `notify_user`
   now rather than a scheduled echo of the answer.
6. **One firing path.** The sweep delivers exactly what `notify_user`
   delivers (feed row always, device ping unless quiet hours, web-push twin,
   never email) so a reminder and a ping never disagree about channels.

## 5. Phases

### Phase A — Job visibility tools

- `chat/tools/runs.py` (new): `list_user_runs` (`status` default
  `running`, `limit` default 10/max 25; rows: id, agent, status, goal ≤300ch
  from `delegation_task` else `input_data.goal`, started/elapsed, turns,
  steps, todos `{done,total}` + open items ≤5 when closed-run data exists,
  live code-task states best-effort, `needs_approval` when paused with a
  pending HITL row, cost, `/runs` link) plus the shared `_progress_for`
  helper. `effect="read"`, `parallel=True`.
- `chat/tools/agents.py`: `get_agent_run` gains the same progress block
  (goal, todos/tasks/files, cost, approval flag) without changing its
  poll-then-report contract.
- Register in `chat/tools/__init__.py`; `ALWAYS_AVAILABLE` +=
  `list_user_runs`; `tools_config` `system` group names it.
- Tests `chat/tests/test_run_visibility.py`: user isolation, status filter,
  goal sources (top-level vs delegated), progress math, approval flag,
  compactness caps.

### Phase B — Reminder tools (finish the halves)

- `chat/tools/workspace.py`: `schedule_notification` (ISO time; naive read
  in the user's timezone; future-only; ≤1y; `effect="reversible"`, not
  sensitive), `list_scheduled_notifications` (`effect="read"`),
  `cancel_scheduled_notification` (owned-only; `effect="reversible"`).
- `ALWAYS_AVAILABLE` += all three; `system` group names them.
- Tests: validation refusals (past, unparseable, bad repeat, 20-live cap),
  list/cancel ownership, sweep tests (one-shot spends, repeats advance from
  the due time, quiet-hours ping suppression, one bad row never kills the
  sweep).
- `API.md` rows (notifications sweep + tool behaviour).

### Phase C — Verification

- `pytest` (new suites + `datasources`, agent sharing/gallery,
  `tools_config`), `tsc`, `eslint`, frontend suite.
- Manual: "what's running?" in chat during an agent run; "remind me
  tomorrow 9am"; heartbeat + cancel by chat; second user sees neither.

## 6. Everything (owner override 2026-09-23 — built, not deferred)

- **Live todo text for running runs.** §4 rule 3 stood while peeking could
  lie; the build keeps it honest instead: the checkpointer is read
  best-effort (same process, same saver), and the block carries
  `todos_source: live | record | none` — a missing read degrades to turn
  activity rather than failing the tool or inventing a plan.
- **Reminder management UI.** Pending reminders are listed/cancelled on the
  Notifications tab as well as conversationally, over thin HTTP endpoints
  on the same queries the tools read.
- **Email for reminders.** Opt-in per reminder (`email: true` on the tool,
  checkbox in the UI), default off. Explicit user consent is what separates
  this from system nudges — and the global email gate still has the final
  say, so a disabled mailer degrades to feed + ping, never an error.
