# Gap Closure Plan

Status: **implemented 2026-09-24.** All four phases landed; verification at
the bottom. Deviations and extra finds are in §6.
Source: a codebase-wide gap sweep on 2026-09-24 (not a diff review). Spans
`Backend/` and `better-n8n-frontend/`; BrowserOS is parked and out of scope.

## 1. Baseline at the time of the sweep

| Check | Result |
|---|---|
| Frontend → backend wiring (every literal `apiClient.<method>(path)` in `src/`, resolved against the Django URLconf **with method check**) | 165/165 resolve, 0 wrong methods |
| WebSocket paths (`/execution/`, `/hitl/`, `/imagine-agent/`) vs `streaming/routing.py` | all match |
| `tsc -b --force` | clean |
| vitest | 27 files, 209/209 pass |
| `npm run lint` | **6 errors** (baseline was 0) |
| `pytest` (full, no `-x`) | **10 failed**, 3226 passed, 21 skipped — all 10 pass in isolation (80/80) |

The wiring check script is the one described in the `reference_endpoint_wiring_check`
memory, extended to all of `src/` and to HTTP methods (`allowed()` reads
`view.actions` for viewsets, `http_method_names` otherwise).

## 2. Gaps, most severe first

### G1 — Every notification can be emailed, not only the digest
- `workflow_backend/settings/base.py:728` defaults `NOTIFICATIONS_EMAIL_ENABLED`
  to `'True'`; CLAUDE.md documents `False` and "email belongs to the digest alone".
- `notifications/utils.py::create_notification` emails any notification that
  does not pass `send_email=False`; `NOTIFICATIONS_EMAIL_TYPES` empty means all types.
- Two writers do not opt out: `chat/turn/agent.py:1339` (chat tool approval)
  and `eval/supervision.py:262` (review queued).
- Latent today only because prod sets no SMTP backend (console default). The
  day SMTP is configured, users get one email per tool approval.
- `send_notification_email` fires a daemon thread; in tests it outlives the
  case and logs `module 'django.core.mail' has no attribute 'outbox'`.

### G2 — Explore installs packs whose engines are off
- `docker-compose.prod.yml` sets no `WORKSPACE_ENGINE`, `BROWSER_ENGINE`,
  `ESIGN_ENGINE` → all `none`.
- `workspaces/engine.py` is a stub (every call raises); the runtime silently
  withholds `compute`/`shell`/`esign` tools (`agents/agent/runtime.py:376-383`).
- `agents/views/gallery.py` install-pack does no availability check;
  `pages/Templates.tsx` never reads `/api/orchestrator/capabilities/`.
- Result: the **Code** pack (8 agents), the **Web** pack's browser scout and the
  **Paperwork** pack's signatures install as agents that can only talk. Several
  blurbs say "One click, no setup."

### G3 — Backend suite is order-dependent (10 failures)
Fail in the full run, pass alone:
- `chat/tests/test_pipeline.py` — `ChatTurnTests` and
  `PreModelLatencyInstrumentTests`: `test_slash_search_always_searches`,
  `test_web_search_also_fills_the_image_panel`
  (mechanism, found 2026-09-24 while building the concurrency phases: the
  command registry populates lazily on first import of `chat/commands/*`,
  which an API test triggers via view loading. `classify_intent`'s legacy
  `/search` branch strips the prefix only while `_get_command("/search")`
  is None; once the real `search` command registers it returns
  `("chat", text)` and the turn goes through `_resolve_command` instead,
  where the tests' `execute_tool` patch does not reach.)
- `eval/tests/test_api.py` — `RunReadTests::test_run_detail_carries_results_and_grades`,
  `ReviewTests::test_a_verdict_settles_the_run`,
  `ReviewTests::test_queue_shows_what_is_waiting_on_me`
- `agents/tests/test_run_limits.py::WorkerAdmissionTests::test_delegated_runs_do_not_pass_through_admission`
- `chat/tests/test_todos.py::GraphIntegrationTests` — two source-inspection
  tests that received the source of the **wrong function**
  (`_apply_contract`, `_price_fold`), which points at a test that reloads a
  module or patches `linecache`/`inspect`.

### G4 — Lint regressed from zero to 6 errors
| File:line | Rule |
|---|---|
| `components/billing/InsightsCharts.tsx:205` | `react-hooks/immutability` |
| `components/chat/CommandPalette.tsx:58` | `react-hooks/set-state-in-effect` |
| `components/files/EditorByType.tsx:23` | `react-refresh/only-export-components` |
| `components/files/EditorByType.tsx:39` | `react-hooks/set-state-in-effect` |
| `hooks/useCommands.ts:62` | `react-hooks/set-state-in-effect` |
| `pages/Runs.tsx:522` | `react-hooks/set-state-in-effect` |

### G5 — Backend capability with no web UI
- **User memory**: `/api/memory/` and `/api/memory/<id>/` have zero frontend
  callers; the only surface is the `/memory` slash command. No page shows or
  deletes what the assistant knows about the user.
- **Missions**: `src/api/missions.ts` is imported by nothing; no `/missions` route.
- **Code tab**: no HTTP route creates a `CodeProject`; `workspaces/urls.py`
  has only the job webhook. Blocked on a real engine (G2), not on UI.

### G6 — Carried over from the 2026-09-24 diff review
- `better-n8n-frontend/src/components/settings/ScheduledReminders.tsx:38`: cancel
  has no `onError`; a one-time reminder that fires between load and confirm
  answers 404, the dialog stays open, nothing is shown, the list is stale.
- `Backend/docs/API.md:620`: `DELETE /api/notifications/scheduled/{id}/` is
  described as "idempotent 204"; a repeat call is 404 and the test asserts it.

### G7 — CLAUDE.md drift
- Says `/dashboards` is not built — `pages/Dashboards.tsx` and the route exist.
- Says `reviewAgent` is "left in place, still unread" — it is retired
  (`logs/revisions.py::RETIRED_KEYS`, `test_agent_lifecycle.py`).
- Says `shell` is still in `UNSERVED_GRANTS` — the set is now empty.
- Env block shows `NOTIFICATIONS_EMAIL_ENABLED=False` — code default is `True` (G1).

## 3. Plan

### Phase 1 — Correctness and honesty (≈ half a day). Do before any demo.

**1.1 Email is digest-only by default (G1)**
- `base.py`: default `NOTIFICATIONS_EMAIL_ENABLED` to `'False'`; same in `.env.local`.
- Pass `send_email=False` at `chat/turn/agent.py:1339` and `eval/supervision.py:262`.
- Test in `notifications/tests/`: scan `create_notification(` call sites
  outside the digest (`reminders.py`) and user-scheduled reminders
  (`scheduled.py`) and fail if any can email.
- Done when: a chat approval with SMTP configured writes a row and sends nothing.

**1.2 Packs say whether they can run (G2)**
- Factor the engine checks in `agents/views/capabilities.py` into one function
  both it and the gallery call (one predicate, the `visible_servers_sync` rule).
- Gallery/pack listing carries `available: bool` and `unavailable_reason`.
- `POST templates/install-pack/` and `templates/<slug>/install/` answer **409**
  with the reason for a pack/template whose required engine is `none`.
- `Templates.tsx`: "Not available on this server" badge, Install disabled,
  reason in the tooltip; drop "no setup" from blurbs of engine-backed packs.
- Tests: `agents/tests/test_install_pack.py` (409 when engine off, 200 when
  patched on); vitest for the badge.
- Done when: on a default install the Code pack cannot be installed and says why.

**1.3 Reminder cancel (G6)**
- `ScheduledReminders.tsx`: `onError` closes the dialog, invalidates the list,
  toasts "That reminder already fired or was removed."
- `API.md:620`: "204; a repeat call returns 404."

### Phase 2 — A signal you can trust (≈ half a day)

**2.1 Find the polluter (G3)**
- Bisect: run each failing test after successively halved prefixes of the
  full collection order until one earlier test reproduces it.
- Suspects, in order: `importlib.reload` / `linecache` / `inspect` patching
  (explains the wrong-source failures); process-global caches not cleared
  (`resolve_witness` cache, `CredentialManager`, `tools_config.overlay`);
  settings overrides leaking via module-level reads.
- Fix the polluter, not the victims. Done when the full suite is green twice
  in a row, and once with `-p randomly` if available.

**2.2 Lint back to zero (G4)**
- `set-state-in-effect`: derive during render or move the `setState` into the
  event that caused it. `Runs.tsx:522`: read `?request=` from `useSearchParams`
  as the selected id rather than mirroring it into state.
- `EditorByType.tsx:23`: move the non-component export to a sibling module.
- `InsightsCharts.tsx:205`: build a new value instead of mutating.
- Done when: `npm run lint` reports 0 problems; update the lint memory.

**2.3 Email threads in tests (G1 follow-up)**
- Send synchronously when `EMAIL_BACKEND` is locmem (or inject the sender), so
  no thread outlives a test case.

### Phase 3 — UI for built capabilities (≈ 1–2 days)

**3.1 Settings → Memory (G5)**
- `api/memory.ts` over `/api/memory/`; a Settings tab listing facts by
  category with delete per fact and "clear all". No backend change.
- vitest for grouping; mobile layout per the mobile layout contract.

**3.2 Missions page (G5)**
- `/missions` route (lazy), list with status and pause/resume/cancel through
  the existing `api/missions.ts`; create form using the `/goal` confirm sheet.
- Add to the sidebar; run the wiring check after.

**3.3 Code tab — deferred**
- Blocked on a workspace engine (`docs/COMPUTE_ISOLATION_PLAN.md`). Phase 1.2's
  badge is the honest stopgap. Not scheduled here.

### Phase 4 — Docs (≈ 1 hour)
- CLAUDE.md: fix the four G7 claims; add this file to "Internal Documentation".
- `API.md`: pack `available` field and install 409 (Phase 1.2).
- Mark this plan's status as each phase lands.

## 4. Verification after every phase
```bash
# Backend (full, no -x)
python -m pytest -q -p no:warnings
# Frontend
npx tsc -b --force && npm run lint && npx vitest run
```
Plus the wiring check (frontend call sites resolved against the URLconf with
method check) after any route or `api/*.ts` change.

## 5. Out of scope
- BrowserOS (parked).
- A real workspace / browser / e-sign engine — separate plans own those.
- Paid benchmark runs.

## 6. Implementation log (2026-09-24)

- **Phase 1.1 (done).** `base.py` + `.env.local` default `False`; `send_email=False`
  added at `chat/turn/agent.py` (chat approval) and `eval/supervision.py` (review
  queued) — every other call site already opted out. New
  `notifications/tests/test_email_policy.py` scans the tree with `ast` and fails
  on any `create_notification(` outside `reminders.py`/`scheduled.py` that can
  email (skips `venv/`, which turned the first version into a hang). Phase 2.3
  folded in: `send_notification_email` sends inline when `EMAIL_BACKEND` is
  locmem, so no daemon thread outlives a test.
- **Phase 1.2 (done).** `capabilities._engine_live` promoted to the public
  `engine_live` + `unavailable_grants(config)` (the one predicate). Gallery
  listing/detail carry `available` + `unavailable_reason` (public projection:
  `available` only); single install answers 409; `install-pack` skips
  engine-blocked members as `engine unavailable: <reason>` and answers 409
  when *every* setup-free member is blocked (Code and Paperwork packs on a
  default install; Web installs the API runner and skips the scout). The
  `install_packs` management command skips the same members. Explore:
  per-card and per-pack "Not available on this server" badges, installs
  disabled with the reason in the tooltip, 409 sentence surfaced in the toast,
  "no setup" dropped from the web/paperwork blurbs (code never claimed it).
  Tests: `test_install_pack.py` (409 off / 200 on / partial / listing flags;
  pre-existing whole-pack tests now run with engines on) + `lib/packs.ts`
  (`src/lib/__tests__/packs.test.ts`).
- **Phase 1.3 (done).** `ScheduledReminders.tsx` `onError` closes the dialog,
  refreshes the list and toasts "That reminder already fired or was removed."
  on 404; `API.md:620` now reads "204; a repeat call returns 404".
- **Phase 2.1 (done).** One systematic polluter found and fixed: registering
  `/search` as a slash command (`chat/commands/web.py`, imported by any
  earlier test — `test_commands.py` sorts before `test_pipeline.py`, any HTTP
  test loads the URLconf) moved `/search` off the legacy seed path
  (intent=search → web_search runs) onto the registry path, where
  `search_command` pinned only the toolbox and never the intent — so the turn
  searched or not depending on import order. Fixed by pinning
  `intent="search"` (as `/research` already did); regression test
  `test_slash_search_searches_when_commands_are_registered`. The other six
  baseline failures (todos×2, eval×3, run_limits×1) could **not** be
  reproduced: each victim was run after every in-order predecessor prefix
  (agents/, browsing/, chat/, core/, credentials/, datasources/, esign/) and
  passed everywhere, so no polluter remains in the tree for them — consistent
  with mid-run file edits during the baseline sweep (all three todos/run_limits
  failures are `inspect.getsource` assertions, which shift lines exactly so).
  Full suite green twice is the arbiter (verification below).
- **Phase 2.2 (done).** Lint is 0. The baseline's 6 became 5 by the time of
  implementation (both `EditorByType` errors already fixed in the tree; a new
  `Topbar` set-state-in-effect had appeared) — all fixed by deriving during
  render or moving `setState` into the causing event, per the plan.
- **Phase 3.1/3.2 (done).** Settings → Memory tab (`api/memory.ts`,
  `lib/memory.ts` + vitest, `components/settings/MemoryTab.tsx`: grouped
  facts, per-fact delete, clear-all behind a confirm; read-and-delete only, no
  backend change). `/missions` route (lazy) + sidebar entry + `pages/Missions.tsx`
  (live/finished lists, pause/resume/cancel through `api/missions.ts`, create
  form with a `/goal`-style confirm sheet). 3.3 stays deferred (no engine).
- **Phase 4 (done).** CLAUDE.md's four G7 claims fixed; plan status flipped;
  `API.md` carries the pack `available` field and both 409s.
- **Extra finds (not in the plan).** (a) `tsc` was red in the handed-over tree
  (`StandaloneChat.tsx`: the `ChatMessageItem` extraction dropped its import
  and left dead imports) — repaired minimally, now clean. (b)
  `chat/tests/test_reminder_tools.py::test_naive_time_is_read_in_the_users_timezone`
  hardcoded `2026-09-24T09:00:00` — passes before 09:00, fails after (past
  times are refused). Rebuilt relative to now in the user's zone. (c) The venv
  predates `requirements.txt` (faiss-cpu 1.8.0 + numpy 2.4.6, mutually
  incompatible — `import faiss` fails); aligned to the pinned
  faiss-cpu 1.13.2 + numpy 2.4.4, which fixed
  `inference/tests/test_kb_eviction.py`. (d) A first full-suite attempt (4
  failed) collided with another writer editing `eval/` mid-run — the three
  eval failures pass in isolation and the tracebacks show code newer than what
  ran. Two (three) clean runs below were on a quiet tree.

## 7. Verification (2026-09-24, quiet tree)

- Backend, full, no `-x`: **3447 passed, 11 skipped, 0 failed — three times
  in a row** (runs #2–#4; run #1's 4 failures are (d) above plus (c), all
  resolved). Baseline was 10 failed / 3226 passed / 21 skipped; the 10 are
  gone (4 fixed by the `/search` intent pin, 6 not reproducible on any
  in-order prefix).
- Frontend: `tsc -b --force` clean, `npm run lint` 0 problems,
  `npx vitest run` 33 files / 259 passed (baseline 27 / 209; +packs, +memory).
- Wiring check (every literal `apiClient.<method>(path)` in `src/`,
  `${...}` concretised, resolved against the URLconf with method check):
  **84/84 resolve, 0 wrong methods** — covers the new `/memory/`,
  `/missions/` and gallery calls.
