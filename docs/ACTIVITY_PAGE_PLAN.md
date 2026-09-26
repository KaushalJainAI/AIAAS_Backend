# Activity page plan (proposed 2026-09-26; built 2026-09-26, decision A)

**Status 2026-09-26:** built. Decision taken: **A** — missions on Activity are
read-only with Cancel/Delete only (no create form, no pause/resume here).

Goal: `/runs` (Activity) is the one screen where a user sees **everything running,
waiting, scheduled, or recently done** — agent runs, workers, coding tasks, eval
sweeps, eval world generation, live chat turns, upcoming and recent cron/scheduled
jobs, missions, and recent file/content activity — and can **stop it or clean it
up**. Today it is a 1,441-line `pages/Runs.tsx` (verified: file ends at line
1441) that mixes an approval queue, a missions section, and a run list, and shows
only agent runs.

## 1. What is wrong today (verified)

1. **It only sees agent runs.** `logsService.listExecutions` (`Runs.tsx:982`) is
   the only list. Eval sweeps sit behind an opt-in `showEval` toggle (default
   off, `Runs.tsx:918,989` — `caller='eval'` is excluded unless checked). Eval
   world generation, detached coding tasks (`agents/agent/tasks.py` in-memory
   `_tasks`), and live chat turns (`ChatRun` in `chat/turn/runs.py`) are never
   queried. "What is running right now?" cannot be answered here. Orchestrator
   vs worker is only a `GitBranch` icon + `is_delegated` flag (`Runs.tsx:1385`).
2. **Nothing can be deleted.** `logs/urls.py` has 7 GET-family routes plus
   `feedback` PUT/DELETE (thumbs only) — no `DELETE executions/<id>/`, no
   bulk-delete (verified). A run stuck `running` after a crash, or test noise,
   stays for ever. Recovery (`agents/recovery.py`) eventually fails orphans, but
   the user cannot act first. There is also no "Mark as failed" — no such route
   in `agents/views/` (cancel/steer/autonomy only).
3. **Run controls are Stop-only in practice.** Backend offers
   `runs/<id>/cancel|`steer|`autonomy/` (`agents/urls.py:46-52`) plus
   approve/reject/steer by agent. There is **no pause/resume route** — resume is
   implicit (execute+thread_id, or approve/reject resumes). The page's
   `RunControls` on live runs is the only control; paused runs are answered from
   the HITL queue, not from the row.
4. **Missions look live but cannot advance in production, and cannot be
   deleted.** `POST /api/missions/` + list + `pause|resume|cancel` exist
   (`missions/urls.py`, `chat/commands/views.py:262,315,338`). No mission DELETE
   exists. `run_mission_sweep` blocks up to 2 h (`missions/sweep.py:31-34`,
   `async_to_sync(start_agent_run_and_wait)`) and sits in
   `scheduler.NOT_IN_PROCESS` (`agents/scheduler.py:235-244`), so anything
   created by `CreateMissionForm` (`Runs.tsx:628`) never advances on the live
   site — though the form promises "It wakes on its own".
5. **Schedules live elsewhere.** Upcoming cron (`Trigger.next_due_at`,
   `queued_for`, `upcoming` next-firings, `status/status_message`,
   `last_run_id` in `agents/views/triggers.py:150-223`), scheduler liveness
   (`triggers/health/`), and run-now (`triggers/<id>/run/`) already exist on the
   Schedules surface. Activity shows none of it: no upcoming, no recent
   outcome (`last_outcome/last_error/consecutive_failures/last_fired_at`),
   no periodic-job health.
6. **File activity is invisible here.** Copy/move are **synchronous column
   writes** (`inference/vfs.py:1739,1799`, `filesystem.py:411-422` — no
   background job), so there is never a "copying…" live row. But the traces
   exist and are unshown: `DocumentVersion` (what every overwrite kept),
   `CodeChange` (workspace edits per run), `RecentFile` (opened/open-count).
   Office draft renders *are* background (`DRAFT_RENDER_QUIET_SECONDS=30`,
   debounced `spawn()`) with no visible state either.
7. **One file does four jobs** (approvals queue, missions, run list, run
   detail), so each part is hard to reason about.

## 2. Target layout

Five sections, top to bottom, each hidden when empty except History:

1. **Needs you** — pending approvals + `ask_user` questions (existing HITL
   queue, unchanged behaviour, `?request=` deep link kept).
2. **Running now** — one list of every live process of any kind (below).
   Columns: kind chip, name, started, elapsed, spend so far, Open link.
   Controls per row are only what the backend supports (Stop / Open; Steer on
   code-task lanes, Mark-as-failed on stuck runs). Polls every 5 s
   **only while non-empty**; nav badge = Needs-you count + Running-now count.
3. **Scheduled** — upcoming work and liveness, read-only here (Schedules stays
   the editor):
   - Cron/schedule triggers: next due, `upcoming` firings, status message,
     Run-now link. Source: existing `trigger_list` + `triggers/health/`.
   - Waiting missions (`status=waiting/active` with `next_wake_at`).
   - Scheduler liveness banner (down only — per-sweep last-run is not shown
     in v1). No new per-job controls in v1.
4. **Recently done** — schedule outcomes + finished runs, newest first:
   - Recent firings: trigger name, when, `last_outcome/last_error`, link to the
     run (`last_run_id` → `/runs?run=`).
   - Finished/failed/cancelled runs with per-row Delete (below).
5. **Recent files & content** — what changed, not what is running:
   - Recent `DocumentVersion` saves (file, when, source app/agent/restore).
   - Recent `CodeChange` rows (run → paths touched).
   - Recently opened files (`RecentFile.opened_at/open_count`).
   - Pending office drafts (autosaved spec awaiting the 30 s render) if
     trivially readable; otherwise omit in v1 — never invent a job table for
     it. Each row links to the file / run that made it.

`?run=` / `?request=` / `?agent=` / `?status=` / `?failure_category=` deep links
keep working.

## 3. Backend work

### 3a. `GET /api/activity/live/` (new, in `logs/`)

Read-only union of live items for the caller only. Each item
`{kind, id, title, status, started_at, elapsed_s, spend_rupees, href,
actions: [...]}`. `actions` is computed server-side; the UI never offers a verb
the backend refuses. Capped with `truncated: true` (same convention as every
other `@api_view` list).

| kind | source (all owner-filtered) | live statuses | actions |
|---|---|---|---|
| `agent_run` | `ExecutionLog` running/pending/paused (incl. `caller=orchestrator` workers; workers carry parent label) | running, pending, paused | Stop (`cancel/`), Open; Steer/Autonomy where execution id known |
| `code_task` | `agents/agent/tasks.py::_tasks` for the user's lead threads | not-done | Stop (`runs/<id>/cancel/` → `_cancel_code_task`), Steer, Open run |
| `eval_sweep` | `EvalRun` pending/running (no `active/cancelling` — real values are `pending\|running\|awaiting_review\|completed\|failed\|cancelled`) | pending, running | Stop (`eval/runs/<id>/cancel/`), Open |
| `eval_world` | `EvalWorld` status `generating` | generating | Open only (no cancel route exists — do not invent one in v1) |
| `chat_turn` | `chat/turn/runs.py::active_keys(user_id)` | running | Stop (`message/stop/`), Open session |

**In-process caveat (load-bearing):** `ChatRun._runs` and code-task `_tasks`
are per-process memory. On a multi-process deploy the endpoint sees only its
own process's chat turns and code tasks; `ExecutionLog`/`EvalRun`/`EvalWorld`
rows are visible everywhere. Document this on the endpoint and in the UI
empty-state ("live chat turns on this server instance"). No Redis mirror in v1.

Stop reuses existing doors only: agent `runs/<id>/cancel/`, eval
`runs/<id>/cancel/`, chat `message/stop/`. No new cancel paths. Pause/Resume
rows are **not** offered for runs (no such route — resume is implicit).

### 3b. Delete + bulk-delete + mark-failed

- **`DELETE /api/logs/executions/<id>/`** and
  **`POST /api/logs/executions/bulk-delete/`** (`{ids}` or
  `{status: failed|cancelled, older_than_days}`): owner-only, 404 for foreign
  ids, **409 for live runs** (stop first). Cascade turns/steps/trace; **keep
  `CostEntry` rows** so spend totals and caps don't silently drop. Exclude
  `caller='eval'` runs (deleted via their eval run's existing cleanup).
- **`DELETE /api/missions/<id>/`** (new — verified absent): owner-only;
  refuse `active/waiting` with 409 (cancel first), matching the run-delete
  rule. Needed by option A below.
- **Eval already done:** `eval/runs/<id>/` DELETE exists (refuses
  pending/running, 409) + `cancel/` POST. Reuse, don't duplicate.
- **`POST /api/logs/executions/<id>/mark-failed/`**: for a run stuck `running`
  past `maxRunSeconds` + grace. Calls `recovery._fail` for that one row. Not a
  status edit — it runs the same close path the sweep would.
- Update `Backend/docs/API.md` for every new/changed route in the same change.

### 3c. Scheduled + history + files (reuse, don't rebuild)

- Scheduled section: existing `GET triggers/` (fields `next_due_at`,
  `queued_for`, `upcoming`, `status`, `status_message`, `last_run_id`,
  `last_outcome`, `last_error`, `consecutive_failures`) + `triggers/health/`
  + missions list. **No new schedule endpoint in v1.**
- Recently-done: derive from the same two (last outcome fields + finished
  `ExecutionLog` rows). No new table.
- Recent files: existing `document_versions/`, `CodeChange` query, recents —
  expose tiny owner-filtered list endpoints only if the current ones can't
  serve Activity (prefer reuse; cap + `truncated`).

## 4. Missions decision (decided: A)

Pick one before building:

- **A. Hide until real (recommended).** Remove `CreateMissionForm` and the live
  missions section from Activity; show existing missions read-only with
  Cancel/Delete only (Delete is the new route above). Re-enable when missions
  get a detached launch path.
- **B. Make them real now.** Give `run_mission_sweep` a detached launch
  (`start_agent_run` + `spawn`, like triggers use) and move it into
  `PERIODIC_JOBS`. Bigger change; own plan. (Today: beat lists
  `missions.sweep_missions` every 300 s in `settings/base.py:686-689` but the
  in-process loop skips it via `NOT_IN_PROCESS`.)

## 5. Frontend work

- Split `pages/Runs.tsx` → `components/activity/`: `NeedsYou.tsx`,
  `LiveList.tsx` (all five kinds, action matrix per kind), `ScheduledSection.tsx`,
  `RecentlyDone.tsx`, `FileActivity.tsx`, `HistoryList.tsx`,
  `RunDetail.tsx` (moved as-is: turns/steps/cost/revision/delegation),
  `MissionsSection.tsx` (decision A: read-only + Cancel/Delete). Page becomes
  layout + header.
- `api/activity.ts` (live endpoint) + trigger/mission/file reuse; delete/bulk/
  mark-failed in `api/logs`; mission delete in `api/missions`.
- Polling: live list 5 s only while non-empty (runs list keeps its 10 s
  while-live rule, `Runs.tsx:994`; detail 5 s/30 s socket rule,
  `Runs.tsx:1009`; missions 30 s stays).
- Delete/Mark-failed/Clear use in-app `ConfirmDialog`, never
  `window.confirm`. Live rows never offer Delete (stop first — backend 409s
  anyway). Destructive bulk (`Clear failed`, `Clear older than…`) shows counts
  before confirming and keeps cost records (say so in the dialog).
- Nav badge = Needs-you + Running-now. Kind chips reuse `CALLER_LABELS` +
  new kinds (`code task`, `eval sweep`, `world`, `chat`). Keep `?run=` /
  `?request=` deep links; schedule rows link to Schedules, file rows to the
  file/run.
- Empty states say what *is* covered ("no live runs, tasks, sweeps, or chat
  turns on this instance") so an empty page reads as proof, not as a missing
  feature.

## 6. Tests

- Backend: `logs/tests/test_activity_live.py` (each kind appears for owner,
  foreign rows never do, `actions` match status/kind — incl. world = open-only,
  eval-cancel refused when finished, code/chat absent cross-process
  documented); `logs/tests/test_run_delete.py` (owner-only 404, live 409,
  cascade turns/steps, `CostEntry` kept, bulk filters, eval-caller excluded);
  `missions/tests/test_mission_delete.py` (cancel-first 409);
  mark-failed (orphan beyond grace closes via `_fail`).
- Frontend: `src/lib/__tests__/activity.test.ts` — the pure action matrix
  (server verbs ordered/labelled, unknown verbs dropped, open-only worlds).
  The repo convention keeps pure-helper tests beside the helper
  (`src/lib/__tests__/`), not under the component.
- Manual: start agent run + code task + eval sweep + world gen + chat turn →
  all five live → stop each → delete finished → clear failed → triggers show
  upcoming + last outcome → file save appears in file activity.

## 7. Order

1. Missions decision (A/B) → 2. delete endpoints (runs, bulk, mission delete,
   mark-failed) → 3. `/api/activity/live/` → 4. scheduled/history/file reads →
   5. split + new UI → 6. docs (`API.md` rows, `CLAUDE.md` architecture note if
   a pattern changes, frontend guide).
