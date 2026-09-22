# Schedules: make them fire, make them honest, make them simple — plan

Status: implemented 2026-09-22 (Phases 1–4; Phase 5 optional, not started).
Audience: the engineer (or model) implementing it. Each step names the file
and the change. Do the phases in order: **Phase 1 is the real bug, and every
later phase is wasted if schedules don't fire.** Each phase ends with a check.

---

## 0. What is wrong today (found 2026-09-22)

A screenshot of `/schedules` showed the "Weekday briefing" card saying
**Next due: 19d ago**, and directly under it **Wed, Sep 23, 08:00 AM**
(tomorrow). **Last run: —** on both cards. Tracing it found five problems:

| # | Problem | Where | Effect on the user |
|---|---|---|---|
| B1 | **Nothing runs the sweep.** `docker-compose.prod.yml` has no `beat`/`worker` service, and there is no in-process ticker. Production depends on a **host crontab** someone must add by hand (`DEPLOYMENT.md` §5b). Local dev (`runserver`) has no beat either. | `docker-compose.prod.yml`, `DEPLOYMENT.md` §5b | Schedules never fire. There is no error anywhere: rows just stay enabled with a `next_due_at` in the past. |
| B2 | **The card shows two different "next due" values.** The relative text uses the stored `next_due_at` (stale), but the date under it uses `upcoming[0]`, which the serializer recalculates from *now*. | `better-n8n-frontend/src/pages/Schedules.tsx:459-462`, `agents/views/triggers.py::get_upcoming` | "19d ago" next to tomorrow's date. The recalculated date also *hides* B1: the card looks healthy. |
| B3 | **Run now blocks the HTTP request until the whole agent run ends.** `trigger_run_now` → `sweep.fire` → `start_agent_run_and_wait`. | `agents/views/triggers.py:400-437`, `agents/sweep.py::fire` | The button spins for minutes (up to the agent's `maxRunSeconds`), then the proxy may time out. Holds a Daphne worker thread the whole time. |
| B4 | **The cron path costs a process per minute, and can double-fire.** Each `docker compose exec … run_due_triggers` boots a new Django process (~150 MB) in the 384 MB container. `fire` waits for the run, so a long run keeps that process alive while the next minute's one starts. `due_triggers` doesn't lock rows, so two overlapping sweeps can both fire the same slot before either re-arms it. | `agents/sweep.py::due_triggers`, `fire` | OOM risk on the 913 MB box (the same way daphne died on 2026-09-16); a duplicate run is possible. |
| B5 | **The card can't say whether a schedule will work.** Up to five separate warning boxes (unattended, self-disabled, failures, last_error, queued), no link to the run that happened, the timezone printed twice (in the description *and* on the globe line), the cron string always shown, and Delete has no confirmation. | `Schedules.tsx::TriggerCard` | The user has to read a whole card to learn "is this working?", which should be one line. |

Also confusing, but not bugs: the sidebar says **Triggers** while the route is
`/schedules`; the agent builder has its own single-schedule editor
(`AgentBuilder.tsx:1365-1420`, `origin='builder'`) alongside the Schedules
page, so there are two places to edit a schedule.

**What is already good and must be kept:** `ScheduleEditor.tsx` (pickers,
timezone defaulting to the viewer's, live server preview with the next 3
dates), `agents/triggers.py` (the timezone-aware cron walker, DST rules,
`describe()`), the outcome words in `sweep.fire`, and the timezone and
lateness rules. This plan doesn't redesign any of those.

---

## Phase 0 — confirm the production state (read-only, 10 min)

Ask the user before connecting to the box. Then on the EC2 host
(13.127.148.207 — see memory `project_prod_deployment`):

```bash
crontab -l                                   # is the run_due_triggers line there?
tail -n 50 /var/log/aiaas-sweep.log          # has it ever run?
docker compose -f docker-compose.prod.yml exec -T backend python manage.py shell -c "
from django.utils import timezone; from agents.models import Trigger
now = timezone.now()
for t in Trigger.objects.filter(mode='schedule', enabled=True):
    print(t.id, t.name, t.next_due_at, t.last_fired_at, t.last_outcome)
print('overdue:', Trigger.objects.filter(mode='schedule', enabled=True, next_due_at__lt=now).count())"
```

Write down the answer in §9 of this file. Whatever it says, Phase 1 goes
ahead. The goal is that schedules don't depend on a manual crontab.

---

## Phase 1 — schedules fire with zero setup (fixes B1, B3, B4)

**Design:** the backend process runs its own scheduler loop. The prod
backend is one Daphne process (`Dockerfile` CMD), so a loop inside it costs
no extra memory and needs no broker or crontab. A **database lease** makes
sure only one process runs the sweep at a time, even if someone scales
Daphne, still has the old crontab, or runs `run_due_triggers` by hand. A
**per-slot claim** makes sure one slot can never fire twice.

### 1.1 Model: `SchedulerLease` (new, in `agents/models.py`)

```python
class SchedulerLease(models.Model):
    """Which process is running the periodic sweeps, and when it last did.

    One row per loop name (`'triggers'`). The holder renews it every tick;
    anyone may take it once `expires_at` has passed. The `beat_at` column is
    what the UI reads to say "the scheduler is not running" instead of
    showing a card that looks healthy while nothing fires.
    """
    name = models.CharField(max_length=40, primary_key=True)
    holder = models.CharField(max_length=80)
    expires_at = models.DateTimeField()
    beat_at = models.DateTimeField()
```

Migration: `python manage.py makemigrations agents`. The app label is
`orchestrator`, so the file lands in `agents/migrations/` and its
dependencies say `('orchestrator', …)`. That is expected.

Also add `last_execution = models.ForeignKey('logs.ExecutionLog',
null=True, blank=True, on_delete=models.SET_NULL, related_name='+')` to
`Trigger`, in the same migration. Phase 2 links to it.

### 1.2 `agents/scheduler.py` (new): the loop

```python
LEASE_NAME = 'triggers'
TICK_SECONDS = 30          # cron resolution is 1 minute; 30s means never late by >30s
LEASE_SECONDS = 90         # 3 missed ticks before another process may take over
HOLDER = f'{socket.gethostname()}:{os.getpid()}:{uuid4().hex[:6]}'

def try_acquire(now) -> bool:
    """Take or renew the lease. One conditional UPDATE, so it is atomic on
    SQLite and Postgres alike — no advisory locks, no select_for_update."""
    SchedulerLease.objects.get_or_create(
        name=LEASE_NAME,
        defaults={'holder': '', 'expires_at': now, 'beat_at': now})
    return SchedulerLease.objects.filter(name=LEASE_NAME).filter(
        Q(holder=HOLDER) | Q(expires_at__lt=now)
    ).update(holder=HOLDER, expires_at=now + timedelta(seconds=LEASE_SECONDS),
             beat_at=now) == 1

async def run_forever():
    while True:
        try:
            now = timezone.now()
            if await sync_to_async(try_acquire)(now):
                await sweep_once(now)          # 1.4
        except Exception:
            logger.exception('[Scheduler] tick failed')   # never let the loop die
        await asyncio.sleep(TICK_SECONDS)

_started = False
def ensure_started():
    """Idempotent. Called from the ASGI app on the first request."""
    global _started
    if _started or not settings.SCHEDULER_ENABLED:
        return
    _started = True
    background.spawn(run_forever(), name='scheduler')
```

Settings (`settings/base.py`): `SCHEDULER_ENABLED =
os.environ.get('SCHEDULER_ENABLED', 'True') == 'True'`. In
`settings/test.py`: `SCHEDULER_ENABLED = False`, because tests must never
start a background loop.

### 1.3 Start it: `workflow_backend/asgi.py`

Daphne doesn't send ASGI `lifespan` events, so start the loop on the first
HTTP request. The Docker healthcheck hits `/api/health/` every interval, so
the first request comes within seconds of boot.

```python
class _StartScheduler:
    def __init__(self, app): self.app = app
    async def __call__(self, scope, receive, send):
        from agents.scheduler import ensure_started
        ensure_started()
        return await self.app(scope, receive, send)

application = ProtocolTypeRouter({
    "http": _StartScheduler(django_asgi_app),
    ...
```

`background.spawn` (not `asyncio.create_task`) is required. CLAUDE.md
"Detached background tasks" explains why: a task created inside a request
inherits an executor that dies with that request.

### 1.4 Split `sweep.fire` into three parts, so it can be non-blocking

Today `fire` does gating → re-arm → **blocking run** → record. Split it
without changing any rule:

- `prepare(trigger, now) -> str | Launch` (sync). Everything in `fire` up to
  and including the pre-run `_rearm`, **plus the slot claim below**. It
  returns an outcome word (`'late'`, `'busy'`, `'paused'`, …) when nothing
  should run, or `Launch(goal=...)` when a run should start.
- `record_started(trigger, now, execution_id)` (sync). The success block at
  the end of `fire`, plus `trigger.last_execution_id = execution_id`.
- `record_failure(trigger, now, outcome, error)`. This is today's
  `_note_failure`, renamed or wrapped.

The existing sync `fire()` becomes `prepare` → `async_to_sync(
start_agent_run_and_wait)` → `record_*`, so `manage.py run_due_triggers`
and its tests behave exactly as before.

**The slot claim (fixes the double-fire in B4).** In `prepare`, replace the
plain pre-run `_rearm` with a conditional update:

```python
claimed = Trigger.objects.filter(
    id=trigger.id, next_due_at=trigger.next_due_at,   # the value we read
).update(next_due_at=new_next, last_outcome='', last_error='')
if not claimed:
    return 'busy'   # someone else took this slot between our read and now
```

`_rearm`'s "no next slot → disable" branch must still run. Compute
`new_next` with the same `next_run_after(...)` call `_rearm` uses, and if
it is `None`, fall through to `_rearm` as today. Don't claim a row that is
only in the sweep because of `queued_for`. For those, filter on
`queued_for=trigger.queued_for` instead.

The **async sweep** in `agents/scheduler.py`:

```python
async def sweep_once(now):
    triggers = await sync_to_async(lambda: list(due_triggers(now)))()
    for t in triggers:
        step = await sync_to_async(prepare)(t, now)
        if not isinstance(step, Launch):
            continue
        try:
            eid = await start_agent_run(t.subagent, step.goal, user=t.subagent.user,
                                        trigger_type='schedule', caller='trigger')
        except AgentRunRefused as exc:
            await sync_to_async(record_failure)(t, now, 'refused', str(exc)); continue
        except Exception as exc:
            logger.exception(...)
            await sync_to_async(record_failure)(t, now, 'failed', f'{type(exc).__name__}: {exc}'); continue
        await sync_to_async(record_started)(t, now, eid)
```

`start_agent_run` is the detached one: it returns once the run has started,
and the run carries on in the background on this long-lived loop. That is
the loop `start_agent_run_and_wait`'s docstring says the sweep never had.
So one slow agent no longer delays every other schedule.

### 1.5 Run now returns immediately (fixes B3)

Rewrite `trigger_run_now` as an **async view**, the same shape as
`webhook_receive` in the same file, which already calls `start_agent_run`
directly from an async view:

1. Load and check the row (ownership, `mode == 'schedule'`, `enabled`)
   through `sync_to_async`. Keep the existing error messages.
2. `step = await sync_to_async(prepare_manual)(trigger, now)`. This is a
   variant of `prepare` that **skips the slot claim and does not re-arm**.
   A manual run is extra, not the next scheduled slot, so it must not move
   `next_due_at`. It still applies the paused-agent, overlap and no-goal
   rules, so the button still tests the real path, which is what its
   docstring promises.
3. If it returned a word, answer `200 {'outcome': word, ...}` as today.
4. Otherwise `eid = await start_agent_run(...)` (refusal →
   `record_failure`), then `record_started`, then answer **`202
   {'outcome': 'fired', 'execution_id': eid, 'trigger': ...}`**.

### 1.6 Scheduler health endpoint

`GET /api/orchestrator/triggers/health/` → `{"running": bool,
"last_tick_at": iso|null}`, where `running = beat_at > now - 2 *
LEASE_SECONDS`. Put it next to `schedule_preview` in
`agents/views/triggers.py` and `agents/urls.py`. **Update
`Backend/docs/API.md`** (CLAUDE.md requires it for every route change).

### 1.7 Retire the crontab

In `DEPLOYMENT.md` §5b, replace the "add this crontab" instructions with:
"Schedules run inside the backend process; no crontab is needed. If you
previously added the `run_due_triggers` line, remove it: it boots a Django
process per minute in a 384 MB container. The lease makes it harmless but
not free." Keep `manage.py run_due_triggers` for debugging. Leave the HITL
reminder and other cron lines alone. Moving those sweeps onto this loop is
Phase 5.

Update `CLAUDE.md` in the "Triggers are back, invocation-shaped" paragraph:
the sweep now runs in-process via `agents/scheduler.py` under a DB lease;
beat and `run_due_triggers` remain as alternatives, and the per-slot claim
makes running several of them at once safe.

### Phase 1 check

```bash
cd Backend && pytest agents/tests/test_triggers.py agents/tests/test_schedules.py agents/tests/test_scheduler.py -q
```
Then for real: `python manage.py runserver`, hit any page, create a
schedule "every 5 minutes" on an agent with *may run unattended* on, and
watch `/runs`. A run must appear within 5.5 minutes without anything else
running. Then click **Run now**: the response must come back in under 2
seconds with an `execution_id`.

---

## Phase 2 — the card tells the truth (fixes B2)

### 2.1 One status per trigger, computed on the server

Add `status` and `status_message` to `TriggerSerializer`
(`agents/views/triggers.py`). Pick the **first** that applies, in this order:

| `status` | When | `status_message` (exact text) |
|---|---|---|
| `scheduler_down` | schedule, enabled, and the health check (1.6) says not running | "The scheduler isn't running, so this won't fire. It restarts with the backend." |
| `needs_permission` | `not agent.allow_unattended` | "{agent} isn't allowed to run on its own yet." |
| `agent_paused` | agent status `paused`/`archived` | "{agent} is paused, so this is skipped until you resume it." |
| `self_disabled` | not enabled and `consecutive_failures >= 5` | "Turned itself off after 5 failures in a row. Last error: {last_error}" |
| `ended` | `last_outcome in ('expired','stopped')` and not enabled | "This schedule has ended." |
| `paused` | not enabled | "Paused." |
| `not_started` | `window_state(now) == 'pending'` | "Starts {starts_at, local}." |
| `overdue` | `next_due_at < now - 2 min` | "Overdue: it should have run {relative}. If this stays, the scheduler is behind." |
| `failing` | `consecutive_failures > 0` | "Last try failed ({n} in a row, turns off at 5): {last_error}" |
| `ok` | otherwise | "" |

To avoid one lease query per row, compute the health **once** in
`trigger_list` and pass it through `serializer context`.

### 2.2 Card changes (`better-n8n-frontend/src/pages/Schedules.tsx::TriggerCard`)

- **Next run** (rename from "Next due"): relative *and* absolute both from
  `next_due_at`. Delete the `upcoming[0]` line. That line is B2.
- **Last run:** relative `last_fired_at` plus the outcome label, linked to
  the run when `last_execution` is set (`/runs?execution=<id>`; check the
  Runs page's actual query param with `grep -n "searchParams" src/pages/Runs*.tsx`).
- **Replace** the five warning blocks (unattended, self-disabled, failure
  count, `last_error`, and the `lastCopy` label when it is an error) with
  **one status row** driven by `status` / `status_message`: green dot for
  `ok` (show nothing else), amber for `overdue`/`failing`/`not_started`/
  `agent_paused`, red for the rest. Keep the queued line.
- Each non-ok status gets **one fix button** where a fix exists:
  - `needs_permission` → **"Allow it"**: `PATCH` the agent with
    `allowUnattended: true` (find the agent update call in `src/api/agents.ts`),
    after a confirm sheet: "Let {agent} run when nobody is watching? It will
    still stop and ask before anything it's set to ask about."
  - `agent_paused` → **"Resume agent"** (PATCH agent status `active`).
  - `self_disabled` / `paused` → **"Turn back on"** (the existing toggle mutation).
  - `scheduler_down`, `overdue` → no button. Also show a page-level banner
    (2.3).
- Card body: the description only ("Every weekday at 08:00 (Asia/Kolkata)").
  **Remove** the always-visible cron line and the separate globe/timezone
  line. The description already names the zone, so the screenshot showed
  it twice. Show the cron only inside the editor's "Custom" tab.
- **Delete** asks first: "Delete this schedule? This can't be undone."
- **Run now** → rename **"Run once now"**. On 202 show "Started · Open run →"
  linking to the execution. Remove the "watch it on Runs" text-only hint.

### 2.3 Page banner

In `Schedules()`, query `/triggers/health/` (refetch 60s). If
`running === false` and at least one schedule is enabled, show one red
banner above the grid: "Schedules are not running right now. The
scheduler last checked in {relative(last_tick_at) or 'never'}." This is
the message that would have caught B1 on day one.

### Phase 2 check
`npm run build`, `npm run lint` (baseline is zero, so any new problem is a
regression), `npx vitest run`, `npx tsc -b --force`. Then load `/schedules`
with the backend's scheduler disabled (`SCHEDULER_ENABLED=False`): the
banner and `scheduler_down` must show. Re-enable it: they clear within a
minute.

---

## Phase 3 — simpler to set up (B5, and the two-editors confusion)

### 3.1 Naming
- Sidebar (`components/layout/Sidebar.tsx:208`): label **"Schedules"**.
- Page title "Schedules", subtitle "Run agents automatically, on a timetable
  or when another app calls a link".
- Grid split into two headed sections, **Schedules** and **Webhooks**
  (webhooks only if any exist). The primary button is **"New schedule"**
  and skips the mode picker entirely. A quieter "New webhook" link opens
  the webhook form. The picker keeps only the agent choice (use
  `NewTriggerPicker` with the mode buttons removed and `mode` passed in).

### 3.2 The editor (`components/schedules/ScheduleEditor.tsx`)
- **Repeats** chips, in this order, with plain labels: *Every day*,
  *Weekdays*, *Weekly*, *Monthly*, *Every few hours*, *Every few minutes*,
  *Custom*. Check `KIND_LABELS` and `ScheduleKind` in `src/lib/cron.ts`; if
  there's no weekdays kind, add one that compiles to `m h * * 1-5`. Add it to
  `fromCron`/`toCron`, and add a case to `src/lib/__tests__/cron.test.ts`.
  **Do not change `describe()` wording.** It is pinned word-for-word to the
  backend's (`agents/tests/test_schedules.py::DescribeTests.CANONICAL`); if
  you must change it, change both tables together.
- Replace the two number inputs (hour, minute) with one
  `<input type="time" step="60">`, which shows the user's own 12/24-hour
  format. Hourly keeps a single "minutes past the hour" number.
- Put **Goal** ("What should it do each time?") *outside* the collapsed
  "advanced" section, directly after the time. Every schedule needs one
  (or the agent's brief), and the Weekday briefing card shows it's the
  field people actually fill. Keep name, start/end window and overlap
  collapsed under **"More options"**.
- `needs_permission` in the editor: replace the paragraph telling the user
  to go to the agent's settings with the same **"Allow it"** button as 2.2,
  inline. Nobody should have to leave the form to make it work.

### 3.3 One editor, not two (agent builder)
In `AgentBuilder.tsx` "When it runs" (~line 1365), replace the embedded
cron editor with a **list of this agent's schedules**
(`triggersService.list()` filtered by `subagent === agent.id`), each showing
its description + status dot, plus **"Add schedule"**. Both open the same
modal as the Schedules page. To do that, move `TriggerModal` out of
`Schedules.tsx` into `components/schedules/TriggerModal.tsx`, with no
behaviour change, and import it in both places. For an unsaved new agent
(no id yet), show "Save the agent first to add a schedule."

Backend compatibility: **keep** the serializer's `schedule` /
`scheduleTimezone` fields and `sync_schedule` (templates and
`chat/tools/authoring.py::create_agent` still write them). The builder just
stops sending them. Once nothing in `src/` sends `schedule`, check with
`grep -rn "schedule:" src/pages/AgentBuilder.tsx`.

### Phase 3 check
Same frontend checks as Phase 2. Manually, starting from a fresh agent:
create a schedule from the agent page, see it on `/schedules`, edit it
there, see the change back on the agent page. It should take **one modal
and no trip to another page**, including granting "may run unattended".

---

## Phase 4 — tests (write them with each phase)

Backend, new file `agents/tests/test_scheduler.py`:
1. `test_lease_single_holder`: two holders. The first `try_acquire` wins
   and the second fails until `expires_at` passes, then the second wins.
2. `test_slot_claimed_once`: call `prepare` twice on the same stale row
   instance. The first returns `Launch` and the second returns `'busy'`,
   and `next_due_at` moved exactly once.
3. `test_sweep_once_starts_detached`: patch `start_agent_run` to return
   `'e1'`. After `sweep_once`, `last_execution_id == 'e1'`, `last_outcome
   == 'fired'`, and `start_agent_run_and_wait` was **not** called.
4. `test_refusal_counts_as_failure`: `start_agent_run` raises
   `AgentRunRefused`, so `consecutive_failures == 1` and outcome `refused`.
5. `test_run_now_returns_202_without_waiting`: patch `start_agent_run`
   and assert 202 + `execution_id`; assert `next_due_at` unchanged (a
   manual run is not the scheduled slot).
6. `test_status_order`: one trigger per row of the 2.1 table, each gets its
   status. Plus one that matches two rules gets the earlier one.
7. `test_health_endpoint`: stale `beat_at` gives `running: false`.
8. `test_scheduler_disabled_in_tests`: `settings.SCHEDULER_ENABLED is False`.

Existing `test_triggers.py` / `test_schedules.py` must pass **unchanged**.
If one fails, the split in 1.4 changed a rule, so fix the split, not the
test.

Frontend: `src/lib/__tests__/cron.test.ts` gets the weekdays kind round-trip.
Add a small pure helper `statusTone(status)` in `src/lib/triggerStatus.ts`
with a test, rather than inline logic in the card.

---

## Phase 5 — later, same fix for the other sweeps (optional)

HITL reminders (`send_hitl_reminders`), orphaned-run recovery
(`recover_runs`), the recycle-bin purge and missions have the **same**
dependency on a host crontab. Once Phase 1 has run in production for a
week, make `agents/scheduler.py` a small registry, `(name, every_seconds,
sync_fn)`, and run each under its own lease name at its own interval. Run
recovery especially: it is the one sweep that matters most when something
has already gone wrong. Not in this plan's scope.

---

## 7. Out of scope

- Catch-up of missed runs. The one-hour lateness rule stays. When the
  scheduler first starts, the Weekday briefing will show "Skipped — too
  late" once and then run normally. That is correct.
- "Last day of month" schedules (cron can't express them; the editor
  already says so).
- Event-mode triggers (no runtime yet).
- Celery. `beat` stays available locally behind `profiles: [async]`; the
  lease and claim make it safe to run alongside the in-process loop.

## 8. Done means

- [ ] With nothing but `docker compose up` / `runserver`, a 5-minute schedule produces runs.
- [ ] Killing the scheduler (`SCHEDULER_ENABLED=False`) shows a red banner within a minute.
- [ ] No card can show a relative time and a date that disagree.
- [ ] Run once now answers in under 2 s and links to the run.
- [ ] Two sweeps at once (loop + `manage.py run_due_triggers`) fire a slot once.
- [ ] A new user can create a working schedule, including the unattended permission, in one modal.
- [ ] `pytest -q`, `npm run build`, `npm run lint`, `npx vitest run` green; `API.md`, `DEPLOYMENT.md`, `CLAUDE.md` updated.

## 9. Phase 0 findings (fill in)

- Host crontab has `run_due_triggers`: ___ (not checked — implementing from
  an environment with no SSH access to 13.127.148.207; confirm on the box,
  then remove the line per 1.7 regardless of the answer)
- Sweep log last line: ___ (same as above)
- Overdue enabled schedules in prod: ___ (same as above)

Built 2026-09-22 without the Phase 0 answers. Two deviations from the letter
of the plan, both where the letter could not work:

1. `trigger_run_now` answers **202**, so
   `test_triggers.py::RunNowTests::test_it_fires_even_though_nothing_is_due`
   was updated 200 → 202 (+ `execution_id` assertion). 1.5 and Phase 4 test 5
   demand 202; the "passes unchanged" clause covers the sweep *rules*, and
   those all pass untouched.
2. `record_started` **resolves** `last_execution` through the log's own id
   instead of assigning the execution UUID to the FK: the FK targets
   `ExecutionLog.id` (auto PK), not `execution_id`, and a `UUIDField` rejects
   test doubles like `'e1'`. The link is best-effort (a firing must not fail
   over a convenience); Phase 4 test 3 asserts the resolved link against a
   real log row instead of `last_execution_id == 'e1'`.
