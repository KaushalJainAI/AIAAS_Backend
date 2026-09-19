# Evaluation — the plan to production

*Status: implemented 2026-09-19 (code complete; paid runs — baseline, calibration,
gate proof — still need the user's go-ahead). Written as an
implementation brief: every task names the files to change, the behaviour to
produce, the tests to add and the check that says it is done.*

*Background reading, in this order: `Backend/docs/EVALUATION.md` (the engine),
`Backend/eval/benchmarks/README.md` (the benchmark), then the "Evaluation"
paragraphs of the root `CLAUDE.md`. The code facts below were verified against
the tree on 2026-09-19; if a line number has drifted, search for the named
function.*

---

## 0. How to use this document

### 0.1 Goal

Take the evaluation system from "a well-built offline harness someone runs by
hand" to "something that protects every deploy and learns from use". There is
**no real user traffic yet**, so the plan has three tracks:

| Track | Question | Phases |
|---|---|---|
| **A — Trust** | Are today's numbers right, and do they stop a bad deploy? | 1, 2, 3, 4 |
| **B — Learn** | When users arrive, will their runs teach us anything? | 5, 6, (7 later) |
| **C — Compare** | How do we do against public benchmarks, and against the bare model we run on? | 8 |

Work the phases in the order of §10. Each phase ends with a **Done when** check;
do not start the next phase until it holds.

### 0.2 Ground rules for the implementer

These are project conventions (from `CLAUDE.md` and the user's standing
instructions). Breaking one is a defect even if tests pass.

1. **Money.** Any command that calls a real model — `benchmark run`,
   `benchmark calibrate`, anything under Track C — spends the user's
   OpenRouter credit. **Ask the user before running one, and state the expected
   cost.** Unit tests must never reach a provider: stub `llm.access.complete` /
   `run_agent` exactly as `eval/tests/test_runner.py` and
   `eval/tests/test_benchmarks.py` already do.
2. **Tests** live in `<app>/tests/test_<topic>.py` and import the app's own
   modules absolutely (`from eval.models import ...`). Frontend tests are
   `src/<dir>/__tests__/<name>.test.ts` (vitest).
3. **API map.** Every added/changed route, permission or serializer gets its
   row in `Backend/docs/API.md` in the same change.
4. **App label.** The `agents/` package's Django label is `orchestrator`
   (`'orchestrator.SubAgent'` in FK strings, `('orchestrator', ...)` in
   migration dependencies). The `eval/` label is `eval`.
5. **Background work** that outlives a request uses
   `workflow_backend.background.spawn()`, never `asyncio.create_task`.
6. **ORM from async code** goes through `sync_to_async`, following the
   existing pattern in `eval/runner.py`.
7. **Model ids** come only from the `CLAUDE.md` "Model IDs" table: agents under
   test `openrouter` / `deepseek/deepseek-v4.1-flash`; judge `openrouter` /
   `meta/muse-spark-1.3-contributor`. They must stay different
   (`test_the_shipped_models_are_the_ones_claude_md_names`). Never type an id
   from memory.
8. **External datasets are never committed** (licences, and keeping them out of
   training data). They are downloaded at run time into a git-ignored cache.
9. **Version control.** `Backend/`, `better-n8n-frontend/` and `BrowserOS/`
   are separate git repos; the root is deliberately **not** a repo — do not
   `git init` it. Stage explicit paths. Commit only when the user asks.
10. **BrowserOS is parked** — do not touch it.
11. **Frontend checks:** `npm run build`, `npm run lint` (the baseline is zero
    problems; any warning is a regression), `npx vitest run`. For a type check
    use `npx tsc -b --force` — plain `tsc --noEmit` checks nothing here.
12. **Before changing code, run the backend suite once** (`pytest` from
    `Backend/`) and record any pre-existing failures, so a regression can be
    told apart from something already red.

### 0.3 Principles every change must keep

Already true of `eval/`; nothing in this plan may break them.

1. **One door.** Every eval case runs the agent through
   `agents/agent/runtime.py::run_agent`, the function real users go through.
   The *only* sanctioned exception is the "bare model" control in Phase 8.4,
   which exists precisely to measure the difference.
2. **Deterministic first, judge second.** `llm_judge` never decides a case
   alone (`test_a_judge_never_decides_a_case_alone`) and fails closed.
3. **The judge is not the agent's model.**
4. **Guardrail suites hold a 100% bar.** They test our code, not the model.
5. **A number that can still move is not final** — `awaiting_review` stays.
6. **A human verdict overrides without overwriting** — `auto_passed` is kept
   for ever next to `EvalReview.verdict`.
7. **Graders are pure.** They read a `GradeContext` snapshot; they never touch
   the database or start work.
8. **Registration is the schema.** A new grader is one `@grader(...)`
   declaration in `eval/graders.py`; a new external dataset is one adapter
   registration (Phase 8.2). No second list anywhere.

---

## 1. Current state (verified 2026-09-19)

### 1.1 Architecture as built

```
 eval/benchmarks/suites/*.py  (SUITE dicts, code)        /evals page (user-built suites)
            │ benchmarks/install.py → AgentSerializer             │ /api/eval/suites/…
            ▼                                                     ▼
 EvalSuite ── EvalCase(goal, input_data[__workspace__], reference, graders[])
            │ runner.start_suite_run (detached, preflighted) | runner.run_suite_now (awaited)
            ▼ runner.sweep → asyncio.gather under a semaphore (suite.concurrency ≤ EVAL_MAX_CONCURRENCY=4)
 per case:  workspace.prepare → run_agent(caller='api') → workspace.snapshot
            → graders.grade_all(GradeContext) → EvalResult
            → supervision.apply_policy (queue for a person or not)
            ▼
 supervision.recompute → EvalRun.score / passed / grader_agreement / status
            ▼
 benchmarks/report.py → eval/benchmarks/reports/<date>.md + latest.md
```

| Piece | File | Notes |
|---|---|---|
| Models | `eval/models.py` | `EvalSuite`, `EvalCase`, `EvalRun` (pins `revision`), `EvalResult` (FK `execution`), `EvalReview` (one per result). One migration: `eval/migrations/0001_initial.py` |
| Graders | `eval/graders.py` | ~27 graders: text, behaviour, file (`file_*`, `json_value`, `csv_value`, `csv_rows`), `llm_judge`. `grade_all` → `(grades, score, passed)`; `passed` is `None` when there were no graders |
| Runner | `eval/runner.py` | Guardrail refusal (`AgentRunRefused`, `LLMUserActionable`) aborts the sweep; other failures are one errored result |
| Supervision | `eval/supervision.py` | Policies `none, failures, disagreement, sampled, all`; `UNCERTAIN_BAND = (0.35, 0.65)`; `recompute` is the single scorer |
| Workspaces | `eval/workspace.py` | `input_data['__workspace__'] = {root, files, watch}`; reset before every attempt; snapshot capped at 200k chars/file |
| Public façade | `eval/api.py` | `grade_answer`, `grade_execution`, `run_suite_now`, `start_suite_run`, … |
| HTTP | `eval/urls.py`, `eval/views.py` | 12 routes under `/api/eval/` |
| Benchmark | `eval/benchmarks/` | 13 suite files; agents in `agents.py`; `manage.py benchmark list|install|run|report` with `--suite --group --tier core|work --repeats --model --notes` |
| Meta-tests | `eval/tests/test_benchmarks.py` | `GOOD_ANSWERS` known-good text answers; `IDEAL_OUTPUTS` per work case; `test_the_untouched_fixtures_fail` (work tier only) |
| Frontend | `src/pages/Evals.tsx`, `src/api/evals.ts`, `src/components/agents/AgentScorecard.tsx` | |

### 1.2 Last results

- Last **full** scorecard (`eval/benchmarks/reports/latest.md`, 2026-09-17 17:49):
  43/45 cases (96%), guardrails 12/12, $0.29, ~29 min. One of its two misses is
  the `GraphRecursionError` that the `STEPS_PER_ITERATION` fix closed later that
  day, so **the headline predates the fix**.
- Latest run (`reports/2026-09-18_0537.md`) is partial: 6 capability cases,
  5 passed, guardrails not run.
- Some suites never fail (Work: Deep research 5/5 on every attempt; Ops desk
  3/3), so they cannot detect a regression.
- Only one model has ever been under test.

### 1.3 Gaps, with evidence

| # | Gap | Evidence | Fixed in |
|---|---|---|---|
| G1 | Eval runs are indistinguishable from user runs: they count against an agent's spend cap and appear in `/runs`, the agent list stats and insights | `runner._run_case` calls `run_agent(..., caller='api')`; `CALLERS = {'chat','orchestrator','trigger','api'}` (`agents/agent/runtime.py:602`); `_spend_this_month` (`runtime.py:942`) and `_with_stats` (`agents/views/agents.py:746`) filter by user+agent only | Phase 1.1 |
| G2 | A sweep killed by a restart stays `running` for ever | `agents/recovery.py` sweeps `ExecutionLog` only; nothing touches `EvalRun`/`EvalResult` | 1.2 |
| G3 | Judge calls are billed but not recorded | `_llm_judge` discards `completion.usage`; `report._cost` sums `execution.cost_usd` only | 1.3 |
| G4 | The Evals page offers a supervision value the API rejects, and hides one it accepts | `src/api/evals.ts:16` `'none'|'disagreement'|'sample'|'all'` vs `supervision.POLICIES = ('none','failures','disagreement','sampled','all')`; `EvalSuiteSerializer.validate_supervision` 400s on `'sample'` | 1.4 |
| G5 | No accepted baseline and no regression gate; the benchmark runs only when typed | no CI; deploy is `docker build/push` by hand (`DEPLOYMENT.md` §2–3) | 2, 4 |
| G6 | The judge has never been measured against known labels; `grader_agreement` exists only if someone reviews | two runs in `latest.md` still `awaiting_review` | 3 |
| G7 | `disagreement` queues nearly every failure: any mix of passing and failing deterministic graders counts as "graders disagreed" | `supervision.needs_review` | 3.2 |
| G8 | Known-bad answers are never tested: text cases are proven passable (`GOOD_ANSWERS`) but not proven failable; only the work tier checks untouched fixtures fail | `test_benchmarks.py` | 3.3 |
| G9 | No quality signal of any kind is recorded — no feedback model, no structured retry/cancel/steer/reject events, no failure category | backend-wide search | 5 |
| G10 | `grade_execution` has no caller outside `eval/`; `reviewAgent` is stored and unread | backend-wide search | 7 (later) |
| G11 | A bad run cannot become a test case | — | 6 |
| G12 | The Python sandbox cannot read workspace files: data reaches `execute_python` only by the model reading it (`read_file`, capped at `AGENT_FILE_READ_CHARS = 30_000` per call) and pasting it into code | `chat/tools/sandbox.py::execute_python` takes only `code`; `workflow_backend/thresholds.py:150` | **Out of scope here**, but it bounds Track C: see 8.3 |

---

## 2. Phase 1 — Plumbing (~1 day)

### 1.1 Eval runs get their own caller (G1)

**Backend**

- `agents/agent/runtime.py`: add `'eval'` to `CALLERS`. Do **not** add it to
  `UNATTENDED_CALLERS` (a person starts a sweep; `allow_unattended` must not be
  required for benchmark agents).
- `logs/models.py`: add `('eval', 'Evaluation sweep')` to
  `ExecutionLog.CALLER_CHOICES`; generate `logs/migrations/0021_…` (choices-only
  migration).
- `eval/runner.py::_run_case`: `run_agent(..., trigger_type='api', caller='eval')`.
- Spend cap: `_spend_this_month` adds `.exclude(caller='eval')`. A benchmark
  must never exhaust a user's real agent for the month.
- Sweep ceiling instead: new `EvalSuite.max_cost_rupees = PositiveIntegerField(null=True, blank=True)`
  (null = unlimited). In `runner._run_case`, after acquiring the semaphore and
  before starting a case, if the run's spend so far (sum over its results'
  executions via `agents.spend.aggregate_rupees`, plus recorded judge cost from
  1.3) ≥ the ceiling, set `abort`, mark the case `skipped` with
  "sweep cost ceiling reached (₹X of ₹Y)", and let the sweep finish as `failed`
  with that message. Benchmark suites set a ceiling in their SUITE dict
  (`'max_cost_rupees'`), written by `install.upsert_suite`.
- Stats and lists exclude eval by default:
  - `agents/views/agents.py::_with_stats` — both querysets `.exclude(caller='eval')`.
  - `logs/queries.py::execution_page` — exclude `caller='eval'` **unless** the
    caller explicitly passed `caller='eval'`.
  - `logs/queries.py::execution_statistics` and `agent_metrics` — exclude.
  - `logs/queries.py::cost_breakdown` — **include** (it is real money) but
    report it as its own line (`by_caller['eval']`), so the user can see what
    benchmarking cost.
- Recovery: an orphaned `ExecutionLog` with `caller='eval'` is **failed, never
  resumed** (in `agents/recovery.py::sweep_orphaned_runs`): the sweep that would
  grade it is dead, so resuming spends money for nobody.

**Frontend**

- `src/pages/Runs.tsx`: a "Show evaluation runs" toggle that passes
  `caller=eval` to the list call. Default off.

**Tests**

- `agents/tests/test_regressions.py::AgentStatsTests` — an eval run is not
  counted in runs or spend.
- `agents/tests/test_agent_runtime.py::SpendCapTests` — eval spend does not
  trip the cap.
- `eval/tests/test_runner.py` — the runner passes `caller='eval'`; a suite
  with `max_cost_rupees` stops and skips the rest; the run is `failed` with
  the ceiling message.
- `logs/tests/…` — `execution_page` hides eval by default, shows it on request;
  `cost_breakdown` includes it under `eval`.
- `agents/tests/test_recovery.py` — an orphaned eval execution is failed, not resumed.

### 1.2 Sweeps survive a restart (G2)

- New `eval/recovery.py::sweep_orphaned_eval_runs()`:
  - Candidates: `EvalRun(status='running')` whose `updated_at` is older than
    `EVAL_ORPHAN_SECONDS` (new constant in `workflow_backend/thresholds.py`,
    default 3 h — longer than any legitimate sweep; the full benchmark's
    slowest suite attempt is ~4 min, a 200-case user suite at concurrency 2
    with 10-min cases would be the extreme).
    Using `updated_at` works because every `_save_result` touches its run's
    results and `_finish` saves the run; also refresh `run.updated_at` when a
    case finishes (one `EvalRun.objects.filter(pk=…).update(updated_at=now())`
    in `_save_result`) so a long but live sweep is never mistaken for dead.
  - For each: results in `running`/`pending` → `status='error'`,
    `error_message='interrupted — the process running this sweep stopped'`;
    run → `status='failed'`, same message; then `supervision.recompute(run)`.
  - Re-read under the write, as `agents/recovery.py::_fail` does.
- Wire it exactly like the run sweep, both doors:
  - call it from the `orchestrator.recover_runs` Celery task
    (`agents/tasks.py`) and from `manage.py recover_runs`
    (`agents/management/commands/recover_runs.py`), **importing
    `eval.recovery` lazily inside the function** so `agents` never imports
    `eval` at module scope.
- **Tests:** `eval/tests/test_recovery.py` — stale run is closed and its
  results errored; a recently-updated run is left alone; a run that finished
  between read and write is not overwritten.

### 1.3 The judge's cost is recorded (G3)

- `eval/graders.py`: `Grade` gains `tokens: int = 0` and
  `cost_usd: Decimal | None = None`; `as_dict` includes `tokens` and
  `cost_usd` (as a string). `_llm_judge` fills them from `completion.tokens` /
  `completion.usage`, priced the same way the runtime prices a turn — read
  `llm/pricing.py::cost_for_usage` and mirror its use in
  `agents/agent/runtime.py::_roll_up_cost`. A judge that errored records 0.
- `eval/models.py::EvalResult`: `judge_tokens` (int, default 0) and
  `judge_cost_usd` (Decimal, null). Filled in `runner._run_case` by summing
  the grades. Migration `eval/0002_…`.
- `EvalRun.tokens_used` = agent tokens + judge tokens (runner `_finish`).
- `eval/benchmarks/report.py::_cost` adds `judge_cost_usd`; the headline
  prints agent cost and judge cost separately; **delete** the "judge calls are
  not recorded" sentence.
- **Tests:** `eval/tests/test_graders.py` — a stubbed judge completion with
  usage produces a grade carrying tokens/cost; `test_runner.py` — the result
  and run totals include it.

### 1.4 Fix the supervision picker (G4)

- `src/api/evals.ts:16`: `SupervisionPolicy = 'none' | 'failures' | 'disagreement' | 'sampled' | 'all'`.
- `src/pages/Evals.tsx`: `SUPERVISION_HELP` keys follow; add help text for
  `failures`; show `sample_percent` input when `sampled` is chosen.
- Add a vitest in `src/api/__tests__/evalPolicies.test.ts` that pins the list
  to `('none','failures','disagreement','sampled','all')` with a comment naming
  `eval/supervision.py::POLICIES` — same pinning pattern as
  `src/lib/__tests__/cron.test.ts`.

### Phase 1 — Done when

- A sweep interrupted mid-run is closed by the next `manage.py recover_runs`.
- `/runs`, the agent list and the spend cap ignore benchmark traffic; the cost
  breakdown shows it as its own line.
- The scorecard's cost includes the judge.
- Creating a suite with each supervision policy from the UI succeeds.
- `pytest` green except pre-recorded failures; frontend build/lint/vitest green.

---

## 3. Phase 2 — A baseline (~½ day; one paid run)

### 2.1 Baseline as data, not a markdown file

- `EvalRun.is_baseline = BooleanField(default=False, db_index=True)` (migration).
- A baseline is identified by **(suite name, agent provider/model)** — the same
  suite on another model has its own baseline.
- `manage.py benchmark accept --user <email> [--suite …]`: for each selected
  suite, takes the latest set of attempts (same selection `benchmark report`
  already uses: the latest N runs of the suite, N = its repeats), refuses if any
  of them is not `completed` (a baseline cannot have pending reviews), clears
  `is_baseline` on that suite+model's previous runs, sets it on these.
- `eval/queries.py::baseline_for(user, suite_name, provider, model) -> list[EvalRun]`.
- `report.render` gains a "vs baseline" column: pass@1 now, pass@1 at baseline,
  delta.

### 2.2 Run it

- **Ask the user first** (expected ≈ $0.30–0.40 including the judge, ~30 min).
  `python manage.py benchmark run --user <email>` (all groups, default repeats),
  then work any review queue on `/evals`, then `benchmark accept`.
- Commit the resulting report under `eval/benchmarks/reports/`.

### Phase 2 — Done when

Every benchmark suite has a baseline on `deepseek/deepseek-v4.1-flash`, all
guardrail suites at 100%, and `latest.md` shows the vs-baseline column.

---

## 4. Phase 3 — Check the checkers (1–2 days; one paid calibration)

### 3.1 Judge calibration, offline

- Data: `eval/benchmarks/calibration/judge_set.py` — a list of rows
  `{id, goal, rubric, answer, tool_trace, label: True|False, kind}` where `kind`
  ∈ `good | bad | subtle`. At least **30 rows**, at least 10 of them `subtle`:
  a fabricated citation, a number with a moved decimal (the `4.8`→`48` failure
  documented in `docs/VISION_AGENT.md`), a correct-sounding answer the tool
  trace could not have produced, an answer that satisfies half a two-part
  rubric, a refusal where an answer was possible, a right answer with a
  hedged/unasked-for extra claim that is wrong.
- Model: `eval.JudgeCalibration` — `judge_provider`, `judge_model`, `n`,
  `agreement`, `false_pass_rate`, `false_fail_rate`, `details` (JSON list of
  `{id, label, score, passed, reason}`), `source` (`handwritten | gold`, see
  8.1), `created_at`. Migration.
- `manage.py benchmark calibrate --user <email> [--source handwritten|gold|all]`:
  runs `_llm_judge` over every row (through `graders.grade_all` with a
  one-element spec list, so it is the exact production path), writes a
  `JudgeCalibration`, prints the three rates and every disagreement.
- Endpoint `GET /api/eval/judge/calibration/` → latest per source (auth; the
  calibration is platform-wide, not per user, so it is read-only for everyone).
  Update `docs/API.md`.
- `/evals` page: a small "Judge" card — model, agreement, false-pass rate,
  date. `report.render` prints the same line in the scorecard header.
- **Targets:** agreement ≥ 90%, **false-pass ≤ 5%** (a judge that passes wrong
  answers is worse than none). If missed: first tighten `JUDGE_SYSTEM` or the
  default threshold (`DEFAULT_JUDGE_THRESHOLD = 0.7`) and re-run; if still
  missed, report to the user with the disagreement list rather than changing
  the judge model (the model is the user's decision, `CLAUDE.md`).
- **Tests** (no network): calibrate command with a stubbed judge produces the
  right rates; a judge error counts as a failed grade (fail closed) and is
  reported separately from a disagreement.

### 3.2 Narrow the `disagreement` policy (G7)

New rule in `supervision.needs_review(policy='disagreement')`, in this order:

1. `auto_passed is None` → queue ("no grader could decide this case") — unchanged.
2. Split grades into **judge** (`type == 'llm_judge'`) and **deterministic**
   (every other type; if a `calls_model` grader is ever added, it counts as
   judge — read `graders.REGISTRY[type].calls_model`, do not hard-code the name).
3. If there is a judge and deterministic graders, and
   `judge_passed != all(deterministic passed)` → queue ("the judge and the
   exact checks disagree").
4. If a judge score is inside `UNCERTAIN_BAND` → queue ("the judge was uncertain").
5. Otherwise → do not queue. Deterministic graders disagreeing *with each
   other* is a partial failure, not uncertainty; the old "score was borderline"
   rule is dropped for cases with no judge.

- Update `eval/tests/test_supervision.py` (the existing tests for the old rule
  change deliberately; say so in the test docstring) and the help text in
  `Evals.tsx`.
- Then **work the current queue once** (or ask the user to) so Phase 2's
  baseline is not left with pending reviews.

### 3.3 Every case proves it can fail (G8)

- `eval/tests/test_benchmarks.py`: add `BAD_ANSWERS`, one plausible **wrong**
  answer per case that has text graders (e.g. JSON with `"age": "29"` as a
  string; four bullets; a stated Bitcoin price). New test
  `test_text_graders_reject_a_known_bad_answer`: every such case fails on its
  bad answer **and** on the empty string.
- Extend `test_the_untouched_fixtures_fail` to every suite whose cases carry a
  `__workspace__` (today it iterates only the six work modules; make it iterate
  `ALL_SUITES`).
- Guardrail cases that are graded on behaviour (`tool_not_used`,
  `paused_for_approval`) get a paired test with a `GradeContext` whose
  `tool_trace` contains the forbidden call / `awaiting_approval=False`, and
  must fail.

### Phase 3 — Done when

The latest `JudgeCalibration` (handwritten) shows agreement ≥ 90% and
false-pass ≤ 5%; every text case fails on its bad answer and on empty; the
supervision tests pin the new rule; the review queue is empty.

---

## 5. Phase 4 — Catch regressions automatically (2–3 days)

### 4.1 A smoke tier

- Mark cases with `'tags': [..., 'smoke']` in the suite files. Choose ~10
  cases that are (a) deterministic-only — **no `llm_judge`**, (b) need no
  connector, (c) passed on every attempt of the Phase 2 baseline (pass^k =
  100%), and (d) together touch every agent: assistant, researcher, analyst,
  file clerk(s), the scoped clerk, and every guardrail suite that needs no
  connector.
- `eval/benchmarks/suites/smoke.py` builds `SMOKE_SUITES` from those tagged
  cases: one SUITE dict per *source* suite (same `agent`, same `group`),
  named `'Smoke: ' + source['name']`, slug `'smoke-' + source['slug']`,
  `repeats` 1, `pass_threshold` copied from the source suite. Built by code, not
  copied, so a case edited in its home suite is edited in smoke too.
- `SMOKE_SUITES` is **not** added to `ALL_SUITES` (a full run must not run the
  smoke cases twice). `benchmark run --tier smoke` selects exactly them; add
  `smoke` to the `--tier` choices in
  `eval/management/commands/benchmark.py` and handle it in `_selected`.
- Budget: < 5 min wall time, < $0.05.

### 4.2 The gate

`benchmark run --tier smoke --gate` exits non-zero when the gate fails, and
prints a one-line verdict as the first line of output and of the report.

Rule (smoke tier):

1. Any **guardrail** smoke case fails → gate fails. No retry: a guardrail
   failure is a bug.
2. A **capability** smoke case fails → it is **re-run once** (a new sweep of
   just that case, via a single-case temporary suite or a runner filter —
   add `case_ids: list[int] | None` to `runner.sweep`/`run_suite_now` rather
   than creating rows). The gate fails only if it fails twice. Model noise on a
   stable case is ~1-in-20; two in a row is a signal.
3. Any case **errored** (the agent never answered) → treated as a failure for
   (1)/(2); an outage must not pass the gate.

For the **full** tier, `--gate` compares each suite's pass@1 with its baseline
and fails if any guardrail suite is below 100% or any capability suite drops
more than `GATE_TOLERANCE` (new constant, default **10 points**; revisit after
four weekly full runs show the real noise).

### 4.3 Wire it into the deploy

There is no CI; deploys are manual (`DEPLOYMENT.md` §2). Add a **"Before you
build"** step to `DEPLOYMENT.md` §2:

```bash
cd Backend
pytest -q
python manage.py benchmark run --tier smoke --gate --user <benchmark account>
# only then: docker build … && docker push …
```

The benchmark runs on the **dev machine** against the dev database — never on
the production box (913 MB RAM; see `CLAUDE.md` deployment notes). Document
which account is the benchmark account and that it needs an OpenRouter key.

### 4.4 Harder suites

- Retire from routine runs, or harden, suites whose baseline pass^k is 100% on
  every case *and* whose cases the model could plausibly answer from memory
  (Work: Deep research). Replace with cases whose answers post-date the model
  or require combining two fetched sources; keep the retired cases with
  `is_active` false (install already retires removed cases, never deletes).
- Add a regression case for each failure seen in the wild, each with a comment
  naming the date and the bug:
  - step-budget exhaustion — a work case needing ≥ 25 iterations, graded on
    `no_error` + its files (would have caught `STEPS_PER_ITERATION`);
  - the reconciliation row the agent dropped (already a case; add a second
    fixture variant with a different near-miss);
  - scoped-path read-back ("No such file" on the path `render` returned) —
    a scoped clerk writes then reads back its own file by the path it was given.

### 4.5 Proof the gate works

On a scratch branch, revert `STEPS_PER_ITERATION` to the old
`max_iterations * 2 + 10` sizing and run the smoke gate: it must fail. Restore.
Record the output in the PR/commit description. (Paid run — ask first.)

### Phase 4 — Done when

`--tier smoke --gate` passes on main, fails on the reverted branch, runs in
< 5 min for < $0.05, and `DEPLOYMENT.md` makes it a step before `docker build`.

---

## 6. Phase 5 — Capture judgement (2–3 days)

The **trace** is already complete (`ExecutionLog` → `AgentTurn` → `AgentStep`,
pinned to a `SubAgentRevision`). What is missing is any record of whether a
result was **good**. All new models live in `logs/` (it is the observability
app); chat is referenced by string FK.

### 5.1 Explicit feedback

- Model `logs.Feedback`: `user` (FK, CASCADE), `execution` (FK
  `logs.ExecutionLog`, null, CASCADE), `chat_message` (FK `chat.ChatMessage`,
  null, CASCADE), `rating` (SmallInteger, `+1`/`-1`), `reason` (choices:
  `wrong`, `incomplete`, `slow`, `unsafe`, `ignored_instructions`, `other`;
  blank allowed), `comment` (Text, blank, max 2,000), `created_at`,
  `updated_at`.
  - `CheckConstraint`: exactly one of `execution` / `chat_message` is set.
  - `UniqueConstraint`s: `(user, execution)` and `(user, chat_message)`
    (partial, on non-null) — re-rating is an update, not a second row.
- Routes (`logs/urls.py`, function views, thin; ORM in `logs/queries.py` per
  that app's rule):
  - `PUT /api/logs/feedback/` body `{target: 'execution'|'message', id, rating, reason?, comment?}` → upsert.
  - `DELETE /api/logs/feedback/?target=…&id=…` → clear.
  - Ownership: the target must belong to the requesting user; **every refusal
    is 404** (same no-oracle rule as `inference/filesystem.resolve_folder`).
  - `execution_detail` and the chat message serializer include the caller's
    `feedback` (`{rating, reason, comment}` or null).
  - `docs/API.md` rows.
- Frontend:
  - Chat: thumbs up/down on each **assistant** message
    (`src/components/chat/` — the message action row next to the existing
    actions in `StandaloneChat.tsx`); thumbs-down opens a small popover with the
    reasons and an optional comment.
  - Runs: the same control in the run detail on `/runs`.
  - Optimistic update, revert on error; never block the transcript.

### 5.2 Implicit signals

- Model `logs.RunSignal`: `user`, `execution` (null), `chat_session_id`
  (CharField, blank), `chat_message` (null), `kind`, `detail` (JSON, small),
  `created_at`. Index `(user, kind, -created_at)`.
- `kind` values and **where each is written** (all best-effort: wrap in
  `try/except`, log, never fail the user's action):

| kind | Written in | detail |
|---|---|---|
| `regenerated` | chat send path when the client says it is a regenerate (add optional `regenerate_of: <message_id>` to the stream request; the frontend's "Rewrite prompt / regenerate" action in `StandaloneChat.tsx` sets it) | `{of_message_id}` |
| `steered` | `chat/views.py::steer_message_stream` (and the agent-run steer path if one exists — search `steering.push`) | `{chars, queued}` |
| `steers_returned` | `chat/turn/runs.py::finish` where `steers_returned` is emitted | `{count}` |
| `approval_rejected` | `chat/turn/agent.py::reject_tool_call` and the agent reject view | `{tool}` |
| `approval_granted` | `approve_tool_call` | `{tool, scope}` (`once|session|always` — "always" is a trust signal) |
| `cancelled` | `agents/agent/runtime.py::cancel_agent_run`; `chat/views.py::stop_message_stream` | `{after_ms}` |
| `failed` | `agents/agent/runtime.py::_close_log` when `status in ('failed','timeout')` | `{category}` (5.3) |

- A shared helper `logs/signals_api.py::record_signal(user_id, kind, *, execution_id=None, session_id='', message_id=None, **detail)`
  (sync) + `arecord_signal` (async wrapper). One writer; the call sites are
  one line each. (Do not name the module `signals.py` — Django convention
  reserves that for model signal receivers.)

### 5.3 Failure categories

- `ExecutionLog.failure_category` CharField(blank, choices): `provider`,
  `step_budget`, `tool_error`, `guardrail`, `contract`, `timeout`,
  `cancelled`, `interrupted`, `other`.
- `logs/failures.py::classify(status, error_message, exc=None) -> str`, called
  in `agents/agent/runtime.py::_close_log` (the single close point for every
  terminal path) and in `agents/recovery.py::_fail` (`interrupted`). Classify
  from exception types first (`GraphRecursionError` → `step_budget`,
  `AgentTurnFailed`/`LLM*` → `provider`, `AgentRunRefused` → `guardrail`),
  message text last.
- Migration backfills nothing (old rows stay blank = unknown).

### 5.4 A place to read it

- `GET /api/logs/insights/quality/?days=30` (`logs/queries.py::quality_summary`):
  counts by `failure_category`, thumbs up/down totals and by reason, signal
  counts by kind, and the 20 most recent thumbs-down targets (id, type, agent,
  reason, comment excerpt). Excludes `caller='eval'`.
- `/evals` page: a **Quality** tab rendering it. No charts needed in v1; if
  one is added, use `components/chat/ChartArtifact.tsx`'s conventions.

### 5.5 Privacy

All new rows cascade-delete with the user. Nothing is sent to a model or
off-box. `comment` is shown only to its author (and staff via admin).

### Tests

`logs/tests/test_feedback.py` (upsert, delete, 404 on a foreign target,
constraint on both/neither target), `logs/tests/test_run_signals.py` (each
writer records exactly one row; a failing write does not fail the action —
patch the helper to raise), `logs/tests/test_failure_category.py` (each
terminal path classifies correctly, including recovery), and a vitest for the
feedback control's optimistic revert.

### Phase 5 — Done when

A day of our own use produces a `quality` response with real counts by
category and at least one thumbs-down with a reason, and nothing in the user
flows got slower or can fail because of a signal write.

---

## 7. Phase 6 — A bad run becomes a test (1–2 days)

- `POST /api/eval/cases/from-run/` body `{execution_id, suite_id?}`:
  - The execution must belong to the user and have a `subagent` (chat turns
    have no agent to replay against — out of scope for v1). 404 otherwise.
  - Suite: the given one (must be the user's), else a per-user suite named
    **"From runs"** created on first use, `subagent` = the run's agent,
    `supervision='all'`.
  - Case: `goal` = `execution.input_data['goal']`; `input_data` = the run's
    input minus runtime keys (`thread_id` and anything starting `_`);
    `reference` = the user's feedback comment if any; `graders = []`;
    `tags = ['from-run', str(execution.execution_id)]`.
  - `graders = []` is deliberate: a case with no graders is queued for a
    person under every policy (`auto_passed is None`), which is exactly right
    for a case whose success criterion has not been written yet. The UI then
    asks for graders.
  - **Files (v1 limitation, state it in the UI):** if the run called
    `read_file` / `list_files`, record the paths it read in the case's
    `reference` as "files this run read". Do **not** build a
    `__workspace__` automatically — the files' *current* contents may differ
    from what the run saw, and a fixture that silently differs is worse than
    none. The author can attach fixtures by hand.
- `docs/API.md` row.
- Frontend: "Save as eval case" button in the `/runs` detail and in the
  thumbs-down popover; on success, navigate to the case on `/evals` with the
  grader picker open.
- **Tests:** `eval/tests/test_case_from_run.py` — ownership 404s, the "From
  runs" suite is created once, runtime keys are stripped, graders empty,
  tags carry the execution id.

### Phase 6 — Done when

A thumbs-down on a run leads, in two clicks, to a case on `/evals` that sweeps
against the same agent and lands in the review queue.

---

## 8. Phase 7 — Score real traffic (later; needs users)

Not to be built now; recorded so Phases 5–6 are built to serve it.

- Sample completed `ExecutionLog`s (not `caller='eval'`) and score them with
  `eval.api.grade_execution`: deterministic graders (`no_error`, `contract`,
  `max_tokens`, `max_duration_ms`) on all, `llm_judge` on a small sample.
- Wire the builder's `reviewAgent` field to it per agent.
- Weekly: review the lowest-scored and thumbs-down runs; save the worst via
  Phase 6.

---

## 9. Phase 8 — External data and benchmarks (3–5 days for the first three)

### 8.0 Why

1. **Volume** — 45 hand-written cases vs hundreds with verified answers.
2. **Judge calibration without human labelling** — gold answers label judge
   verdicts automatically (8.1).
3. **The platform tax** — run the same sample through our agent *and* as a
   bare model call, and publish the difference (8.4). Our own suites cannot
   tell "the model is good" from "our platform is good"; this can.
4. **Realistic fixtures** — public messy data instead of hand-typed CSVs.

### 8.1 Judge calibration from gold answers (with Phase 3)

For datasets with a short gold answer (SimpleQA, GAIA), the dataset's own
matcher labels an agent answer correct/incorrect. Ask the judge the same
question with rubric "The correct answer is «gold». Does the answer state it,
without contradicting it?" and compare. Each row becomes a
`JudgeCalibration(source='gold')` detail entry. This calibrates the judge on
*reference matching*, which is easier than open rubrics — so it supplements
the hand-written set, never replaces it.

### 8.2 The adapter layer

```
eval/benchmarks/external/
  __init__.py      # ADAPTERS: dict[str, Adapter] — registration is the schema
  base.py          # Adapter dataclass + CaseSpec typing
  fetch.py         # download → cache, pin revision, verify sha256
  ifeval.py        # + ifeval_checks.py (ported instruction checkers)
  gaia.py
  simpleqa.py
  dabench.py       # InfiAgent-DABench (see 8.3 for why not DABstep first)
  agentdojo_payloads.py
```

`Adapter` fields: `slug`, `name`, `source_url`, `licence`, `hf_repo`
(or direct URL), `revision` (commit sha or file hash), `files`
(`{name: sha256}`), `agent` (key in `benchmarks/agents.py`), `group`
(`'external'`), `load(cache_dir, *, sample, seed, filters) -> list[case dict]`,
`default_sample`, `gated: bool`, `redact_gold: bool`.

Rules:

- **Same case shape as internal suites.** `load` returns the exact case dicts
  `install.upsert_suite` already accepts (goal, `input_data` incl.
  `__workspace__`, `reference`, `graders`, `tags`), so the runner, workspace,
  report and review queue are unchanged.
- **Dataset scoring is a registered grader** in `eval/graders.py`:
  `ifeval_check` (params: `instruction_ids`, `kwargs`), `quasi_exact_match`
  (params: `value`, `kind: number|string|list` — port GAIA's public
  normaliser), `numeric_match` (params: `value`, `tolerance`). Registered, so
  they are validated on save, listed by `/api/eval/graders/`, and unit-tested.
- **Provenance on every case:** `input_data['__source__'] = {dataset, revision, item_id}`
  (a `__` key, so `_goal_for` never shows it to the agent).
- **Cache:** `settings.EVAL_DATA_DIR` (default `Backend/.eval_data/`); add it to
  `Backend/.gitignore` **and** `Backend/.dockerignore`. `fetch.py` uses
  `huggingface_hub.hf_hub_download` (already in `requirements.txt`) with
  `revision=` pinned; gated sets read `HF_TOKEN` from the environment and fail
  with a clear message if it is missing. Verify the sha256 of every file and
  refuse on mismatch.
- **File formats:** prefer JSONL/CSV/JSON sources. If a dataset is Parquet
  only, add `pyarrow` to a **dev-only** `requirements-dev.txt`, never to the
  production image.
- **Seeded, stratified sampling:** `--sample N --seed S` (defaults per
  adapter, e.g. 30), stratified by the dataset's level/category. Installed as a
  suite named `External: <name> (n=<N>, seed=<S>)` so two samples never merge.
- **Cost guard:** before sweeping, estimate cost as
  `cases × attempts × (agent estimate + judge estimate if any calls_model grader)`
  and refuse above `--max-cost` (default $1) without `--yes`.
- **Gold answers never leave the process in clear:** for adapters with
  `redact_gold=True` (GAIA — its terms ask that answers not be redistributed),
  grader `detail` strings must not include the gold value, `reference` holds it
  only in the DB, and `report.render` prints the item id instead of the
  question or expected answer for `group == 'external'`.
- CLI: `benchmark external list`, `benchmark external install <slug> --sample --seed`,
  and `benchmark run --group external [--suite …]`. External suites are a
  **separate group** on the scorecard and **never part of `--gate`**.

### 8.3 Which benchmarks, in what order

Verify licence, access terms, size and file format of each **before**
writing its adapter — the facts below are as understood on 2026-09-19 and
must be re-checked at the source.

| # | Benchmark | Tests | Our agent | Grading | Notes |
|---|---|---|---|---|---|
| 1 | **IFEval** (`google/IFEval` on HF) | ~500 prompts with verifiable formatting instructions | `assistant` | `ifeval_check` (deterministic) | Port the instruction checkers (Apache-2.0, keep the licence header) into `ifeval_checks.py`; skip instruction types needing extra deps (e.g. language detection) unless the user agrees to a dev-only dep. Cheapest, fully deterministic — **build first**. Also the clearest platform-tax probe: our system prompt may *cost* points here. |
| 2 | **GAIA**, validation split, level 1 first (`gaia-benchmark/GAIA`, gated) | general-assistant questions needing web + files + reasoning | new `generalist` agent: `webSearch`, `scrape`, `sandbox`, `fileOps` (scoped) | `quasi_exact_match` on a `FINAL ANSWER:` line | Attached files become `__workspace__` fixtures **only if text-extractable** (txt/csv/json/py/docx/xlsx→csv); items needing images/audio/video are skipped with a recorded reason — report the skip count. `redact_gold=True`. |
| 3 | **InfiAgent-DABench** | data-analysis questions over small CSVs, closed-form answers | `analyst` + `fileOps` | `numeric_match` / `quasi_exact_match` | Chosen over **DABstep** for the first data set because of G12: the sandbox cannot read files, so data must pass through `read_file` (30k chars/call) into code. Filter to items whose CSV fits in one read. DABstep's large shared files would measure G12, not analysis — adopt it only after the sandbox can mount workspace files (a separate platform change; raise it with the user). |
| 4 | **AgentDojo** attack payloads (`ethz-spylab/agentdojo`) | indirect prompt injection | existing guardrail agents | existing `tool_not_used`, `file_absent`, `not_contains` + canary | **Payloads only** in v1: port ~10 attack templates into new `guard_work`-style cases (injection inside a fixture file, inside a fetched page's text, inside a document the agent is told to summarise). These are **internal guardrail cases** (100% bar, eligible for smoke), not an external suite. The full AgentDojo harness is out of scope. |
| 5 | **SimpleQA** | short factual questions; separates *incorrect* from *not attempted* | `researcher` | `quasi_exact_match` + `llm_judge` with gold | Report three rates: correct, incorrect, not-attempted — an agent told to admit uncertainty should trade incorrect for not-attempted. Main use: gold rows for 8.1. |
| 6 | **FRAMES** (`google/frames-benchmark`) | multi-hop questions over linked Wikipedia articles | new `librarian`: `rag` grant, KB-scoped, **no web** | `quasi_exact_match` + judge | Load the linked articles (pinned revisions) into a KB in the benchmark account as a closed corpus — exercises `inference/` RAG end to end. Costs embeddings; later. |
| 7 | **CUAD** (contracts, CC BY 4.0) | clause identification in real contracts | `work_docs` clerk | `file_contains` / judge | Source of realistic fixtures for the contract-audit work case (8.6), not a standalone suite. |
| — | τ²-bench | policy-following with a simulated user | — | final DB state | **Deferred**: needs a simulated-user loop the runtime does not have. |
| — | BFCL | raw function-call correctness | — | AST match | **Optional**: measures the model, not the platform; useful only when choosing a default model. |

Deliberately **not** adopted: browser benchmarks (WebArena, BrowserGym — no
browser tool), SWE-bench (not a coding product), anything requiring a harness
that bypasses `run_agent`.

New benchmark agents (`generalist`, `librarian`) go in
`eval/benchmarks/agents.py`, pass the serializer
(`test_every_benchmark_agent_passes_the_serializer`) and keep the `[Bench]`
prefix.

### 8.4 The platform-tax control (the one exception to "one door")

- `EvalRun.mode = CharField(choices=[('agent','Agent'),('bare','Bare model')], default='agent')`.
- `runner.sweep(..., mode='bare')`: instead of `run_agent`, call
  `llm.access.complete` once with the **same provider/model** as the agent, the
  case goal as the prompt, a one-line neutral system message ("Answer the
  question. End with a line 'FINAL ANSWER: <answer>'." where the adapter needs
  it), no tools, no workspace. Build the `GradeContext` from the completion
  (empty `tool_trace`, `files={}`), grade with the same graders.
  `EvalRun.subagent` stays set (so the model is known) but no `ExecutionLog` is
  written — say so in the run's `notes`.
- `benchmark run --group external --bare` runs both modes on the same sample;
  `report.render` prints, per external suite: agent score, bare score, delta.
- Document in `EVALUATION.md` why this bypass exists and that it is only
  reachable for `group == 'external'` suites (enforce it in the command).

### 8.5 Contamination

Public sets leak into training data. So: prefer validation splits whose test
counterparts are hidden, prefer newer sets, treat a suspiciously high **bare**
score as a warning, not a win, and keep our own computed-answer work-tier
cases as the primary signal.

### 8.6 Realistic fixtures from public data

Where a work-tier case uses hand-typed data, a pinned slice of real public data
(CUAD contracts; public open-data CSVs) may replace it — fetched through
`fetch.py`, checksummed, never committed, and with `IDEAL_OUTPUTS` still
computed by a reference solution over the fetched text so
`test_the_reference_outputs_pass_every_file_check` keeps holding. Fixture
fetch must be skipped (not failed) in unit tests when the cache is absent.

### 8.7 Cadence

External suites run **monthly** and **whenever the default model changes**,
never in the deploy gate.

### Tests (no network)

- `eval/tests/test_external_adapters.py`: each adapter's `load` over a tiny
  checked-in **synthetic** sample file in `eval/tests/fixtures/external/`
  (hand-written rows in the dataset's schema — not real dataset rows) produces
  valid case dicts; provenance present; sampling is deterministic for a seed.
- `eval/tests/test_graders.py`: `ifeval_check`, `quasi_exact_match`,
  `numeric_match` known-good and known-bad pairs; GAIA-style normalisation
  (`"1,000"` vs `"1000"`, list order, units).
- `fetch.py`: checksum mismatch refuses; missing `HF_TOKEN` on a gated set
  gives the clear message.
- Report: an external result with `redact_gold` never renders the gold value.
- `mode='bare'`: grades a stubbed completion; refused for non-external suites.

### Phase 8 — Done when

IFEval, GAIA-L1 and InfiAgent-DABench each run end to end on a seeded sample
through `run_agent`, the scorecard has an **External** section with agent vs
bare scores and deltas, gold rows feed `JudgeCalibration(source='gold')`, and
the AgentDojo-derived injection cases pass at 100% as internal guardrails.

---

## 10. Order, dependencies and size

```
Phase 1 ──► Phase 2 ──► Phase 3 ──► Phase 4 ──► Phase 8 (2–7)
                          ▲
             8.1 gold calibration can start with Phase 3

Phase 5 ──► Phase 6          (independent of 2–4; start after Phase 1,
                              which introduces caller='eval' that 5.4 excludes)
Phase 7 — later, needs users
```

| Order | Phase | Track | Size | Paid runs (ask first) |
|---|---|---|---|---|
| 1 | Plumbing | A | ~1 d | none |
| 2 | Baseline | A | ~½ d | full benchmark ≈ $0.35 |
| 3 | Check the checkers | A (+8.1) | 1–2 d | calibration ≈ $0.05 |
| 4 | Catch regressions | A | 2–3 d | smoke runs ≈ $0.05 each; one gate-proof run |
| 5 | Capture judgement | B | 2–3 d | none |
| 6 | Run → test | B | 1–2 d | none |
| 8 | External data | C | 3–5 d (first three) | seeded samples, ≤ $1 each by default |
| 7 | Score real traffic | B | later | — |

Roughly three weeks of focused work for Phases 1–6 and 8.

## 11. Documentation to update as you go

- `Backend/docs/API.md` — every route added in Phases 3, 5, 6.
- `Backend/docs/EVALUATION.md` — caller `eval`, sweep recovery, judge cost,
  baseline, the new `disagreement` rule, calibration, the bare-mode exception.
- `Backend/eval/benchmarks/README.md` — `--tier smoke`, `--gate`, `accept`,
  `calibrate`, `external …`, `--bare`, cost figures.
- `DEPLOYMENT.md` — the pre-build gate step.
- Root `CLAUDE.md` — a short paragraph per landed phase in the existing style
  (what changed, why, where the tests are), and mark this plan's status line.
- This file — tick each phase's status line with its date when it lands.

## 12. Open decisions (defaults proposed; confirm with the user)

| Decision | Proposed default |
|---|---|
| Deploy-gate tolerance for the full tier | 10 points pass@1 per capability suite; revisit after four weekly runs |
| Smoke retry rule | capability cases retried once; guardrails never |
| Eval spend and the agent cap | excluded from the cap; bounded per suite by `max_cost_rupees` |
| First data-analysis dataset | InfiAgent-DABench; DABstep after the sandbox can read files (G12) |
| AgentDojo | payloads as internal guardrail cases; no full harness |
| A second model under test | full tier monthly on one additional model the user picks from `nodes_aimodel` (`is_active=True`) |
| The sandbox-reads-files platform change (G12) | out of scope here; raise separately |
