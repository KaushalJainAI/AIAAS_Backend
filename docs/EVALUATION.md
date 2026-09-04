# Evaluation — `eval/`

*Is this agent any good, and who says so?*

`logs/` records what an agent did. `eval/` records whether it should have done
that — and, because a grader is a program with opinions, whether the grader was
right.

---

## 1. The shape

```
EvalSuite ── EvalCase              a named set of cases + a supervision policy
    │
    └── EvalRun ── EvalResult ── EvalReview
      (one sweep)  (one case)    (a person's verdict)
```

| Table | Row is | Key columns |
|---|---|---|
| `eval_evalsuite` | a set of cases and the policy for reviewing them | `supervision`, `pass_threshold`, `reviewer`, `concurrency` |
| `eval_evalcase` | one goal handed to the agent | `goal`, `input_data`, `reference`, `graders` |
| `eval_evalrun` | one sweep of a suite against one agent | `status`, `score`, `passed`, `grader_agreement`, `revision` |
| `eval_evalresult` | one case's outcome | `auto_passed`, `auto_score`, `grades`, `review_state`, `execution` |
| `eval_evalreview` | a person's verdict on a result | `verdict`, `agreed_with_graders`, `corrected_answer` |

The app label is **`eval`, singular**. A previous `evals` app was deleted
2026-08-17 (see API.md §18) and dev databases predating that still carry inert
`evals_*` tables plus an `evals.0001_initial` row in `django_migrations`. The
singular name means a fresh `migrate` cannot collide with the corpse of the old
one.

---

## 2. Six decisions, and what each one prevents

### 2.1 A sweep runs the agent through the same door everything else does

Every case is executed by `agents.agent.runtime.run_agent` with `caller='api'`
— the same entry point a user pressing "run" goes through, under the same
guardrails, writing the same `ExecutionLog`. `EvalResult.execution` is the FK
that keeps the full turn-by-turn trace one hop from the score.

*Prevents:* measuring a code path nobody uses. An eval harness with its own
private execution path eventually diverges from production and then scores
something that does not ship.

### 2.2 A human verdict overrides, it does not overwrite

`EvalResult.auto_passed` keeps the graders' answer for ever;
`EvalReview.verdict` is what the run's score is computed from, through
`final_passed` / `final_score`. Two records rather than one field.

*Prevents:* losing the only number that says whether the suite works.
`EvalRun.grader_agreement` — the fraction of reviewed results where the person
agreed with the graders — is not computable from a column that was overwritten.
A suite whose graders agree with people 55% of the time is measuring noise, and
without this you would never find out.

### 2.3 A run whose score can still move is never `completed`

A sweep with anything queued for review sits in `awaiting_review`, with
`passed = NULL`. `supervision.recompute()` moves it to `completed` only when the
last verdict lands, and it is the one function that computes a score — called
both at the end of a sweep and after every review.

*Prevents:* a provisional score being read as a release decision. It is also
why `queries.agent_scorecard` reports `awaiting_review` separately and leaves
`latest` null until a run settles.

### 2.4 A case nothing could grade does not pass

`graders.grade_all([])` returns `auto_passed = None`, not `True`, and
`supervision.needs_review` queues a `None` under every policy but `none`.

*Prevents:* vacuous truth. An empty suite reporting 100% is the single most
misleading number an eval system can produce.

### 2.5 Failures are per-case; refusals stop the sweep

A case whose agent run raises becomes one `error` result and the sweep carries
on. A **guardrail** refusal or a missing credential aborts it, because every
remaining case would fail identically.

*Prevents:* a two-hundred-row report that says "monthly spend cap reached" two
hundred times. Errored cases still score zero, so an outage drags the score down
rather than disappearing from the denominator.

### 2.6 A run is pinned to the configuration it scored

`EvalRun.revision` is set at open time from `logs.revisions.current(agent)`, the
same way `ExecutionLog.revision` is.

*Prevents:* "it scored 0.9 last week" naming nothing you can go back to. The
scorecard reports the revision number beside each score, which is what makes
"did this prompt change help?" answerable.

---

## 3. Graders

One `@grader(...)` declaration per assertion in `eval/graders.py`. **Registration
is the schema**: `REGISTRY` is what the API validator, the runner, and
`GET /api/eval/graders/` all read, so a grader that can be saved is one that can
be run. Same rule as `chat/tools/`.

| Grader | Asserts | Params |
|---|---|---|
| `contains` / `not_contains` | substring presence | `value`, `ignore_case` |
| `equals` | exact answer | `value`, `ignore_case`, `strip` |
| `regex` | pattern match | `pattern`, `ignore_case`, `negate` |
| `min_length` / `max_length` | answer size | `value` |
| `json_key` | key (and optionally value) in structured output | `key`, `equals` |
| `contract` | the run met its `output_schema` contract | — |
| `tool_used` / `tool_not_used` | what the agent reached for | `tool` |
| `no_error` | the run finished and was not left paused | — |
| `max_tokens` / `max_duration_ms` | budget | `value` |
| `llm_judge` | a model scores the answer against a rubric | `rubric`, `threshold`, `provider`, `model` |

**A case passes when every grader passes.** Graders are assertions, not votes.
`score` is still a weighted mean, because ranking failures is useful and because
the supervision policy reads the mid-band.

**Everything but `llm_judge` runs with no provider, no credential and no
network.** `Grader.calls_model` says which is which, and a test asserts the set
is exactly `{llm_judge}` — a grading pass whose cost is unpredictable is one
nobody runs.

**The judge fails closed.** A provider outage, a missing rubric, or a reply with
no parseable score is a *failed* grade whose detail says the judge was the thing
that broke. A judge that passes everything when it malfunctions turns an outage
into a green suite. It defaults to `EVAL_JUDGE_PROVIDER` / `EVAL_JUDGE_MODEL`
rather than the agent's own model: same-model self-grading is the one
configuration where a rubric failure and an agent failure cannot be told apart.

---

## 4. Supervision

`eval/supervision.py`. The policy lives on the suite and is **copied onto the
run**, so editing a suite does not rewrite what was supervised.

| Policy | Queues |
|---|---|
| `none` | nothing |
| `failures` | every case the graders failed |
| `disagreement` *(default)* | where the graders were least sure |
| `sampled` | `sample_percent` of results, at random |
| `all` | everything |

`disagreement` is the one worth reasoning about. It queues a result when the
graders split (some passed, some failed), when an `llm_judge` score lands in
`UNCERTAIN_BAND` (0.35–0.65), or when the overall score does. That is where a
grader is most likely to be wrong, and it is the only policy whose review cost
does not grow with the size of the suite.

An `unsure` verdict is recorded, not refused: it leaves the grader's verdict
standing and sets `agreed_with_graders = NULL`, so an honest "I cannot tell"
does not drag the agreement figure down. An errored case cannot be reviewed at
all — there is no answer to have an opinion about, and letting one in would
dilute agreement with verdicts on outages.

**One notification per run, never one per result.** A 200-case suite under `all`
would otherwise deliver 200 notifications for a single sitting's work — the same
reasoning as the HITL digest in `notifications/reminders.py`.

---

## 5. Bounds

In `workflow_backend/thresholds.py`. A sweep runs the agent once per case, so
every number here bounds what one button press can cost.

| Constant | Value | Why |
|---|---|---|
| `EVAL_MAX_CONCURRENCY` | 4 | ceiling `suite.concurrency` is clamped to |
| `EVAL_MAX_CASES_PER_SUITE` | 200 | the size of a suite is the size of a bill |
| `EVAL_RESULT_ANSWER_CHAR_LIMIT` | 16k | per-result copy; `answer_truncated` marks a cut |
| `EVAL_RUN_LIST_LIMIT` / `EVAL_RESULT_LIST_LIMIT` / `EVAL_REVIEW_QUEUE_LIMIT` | 100 / 200 / 100 | these are `@api_view` functions, which DRF pagination never reaches |

Cancellation is cooperative: `POST /runs/{id}/cancel/` flips the row and each
case checks it on the way *out* of the concurrency semaphore. A case already
inside a model call runs to completion rather than being abandoned half-paid-for.

---

## 6. Using it from another app

`eval/api.py` is the public surface. Everything else is HTTP plumbing
(`views`, `serializers`, `urls`) or internal to a sweep.

```python
from eval import api as evals

# Pure — no rows, no provider (unless a spec asks for llm_judge)
evals.list_graders()
await evals.grade_answer('It is Paris.', [{'type': 'contains', 'value': 'paris'}])
evals.needs_review('failures', auto_passed=False, score=0.0)
evals.validate_graders(specs)                      # raises GraderError

# Score a run that already happened — chat, a trigger, a delegation.
# Persists nothing; the caller decides if the verdict is worth keeping.
await evals.grade_execution(execution_log, specs, reference='...')

# Sweeps
run    = await evals.run_suite_now(suite, agent, user)    # awaited, already settled
run_id = await evals.start_suite_run(suite, agent, user)  # detached, 202-shaped

# Supervision and reads
evals.record_review(result, reviewer=user, verdict='fail')
evals.review_queue(user), evals.agent_scorecard(user, agent_id)
```

Three properties make this safe to depend on, each with a test in
`eval/tests/test_public_api.py` that fails if it stops holding:

**No module-level import of a sibling app.** Every reference to `agents`,
`logs`, `llm` or `notifications` inside `eval/` is function-local, so `agents`
importing `eval` cannot create a cycle. Both import orders are exercised.

**`eval/__init__.py` is empty on purpose.** `INSTALLED_APPS` makes Django import
the package during setup, before the app registry is ready — so anything there
touching `eval.models` would raise `AppRegistryNotReady` on boot. That is why
the façade is `eval/api.py` and not `__init__.py`.

**The pure layer needs no ORM.** `graders.py` and the policy half of
`supervision.py` import no models at all, so "score this text against these
assertions" is usable by anything, including code with no database in reach.

`grade_answer` returns plain JSON (`{'score', 'passed', 'grades'}`) rather than
`Grade` dataclasses: a caller writing the verdict into its own table should not
have to import ours. And `passed` is `None`, never `True`, when nothing could
decide — a foreign caller inherits that rule rather than having to know it.

The `Eval*` models are importable directly (`from eval.models import EvalSuite`)
and take objects rather than ids throughout, so **ownership stays the caller's
business to have settled** — nothing in the public layer re-checks a user for
you except the `queries.*` reads, which take `user` as their first argument.

---

## 7. Endpoints

Full rows with access, complexity and gotchas are in [API.md](API.md) §20.

```
GET    /api/eval/graders/                     what a case can assert
GET    /api/eval/suites/                      list + per-suite health counts
POST   /api/eval/suites/
GET    /api/eval/suites/{id}/                 suite + its cases
PATCH  /api/eval/suites/{id}/
DELETE /api/eval/suites/{id}/
GET    /api/eval/suites/{id}/cases/
POST   /api/eval/suites/{id}/cases/           graders validated here
GET    /api/eval/cases/{id}/
PATCH  /api/eval/cases/{id}/
DELETE /api/eval/cases/{id}/
POST   /api/eval/suites/{id}/run/             202 + run_id
GET    /api/eval/runs/                        sweep history
GET    /api/eval/runs/{run_id}/               sweep + results + grades + reviews
POST   /api/eval/runs/{run_id}/cancel/
GET    /api/eval/reviews/pending/             the review queue, oldest first
POST   /api/eval/results/{id}/review/         a verdict
GET    /api/eval/agents/{id}/scorecard/       per-suite scores over time
```

---

## 8. Tests

`eval/tests/` — 113 tests.

- `test_graders.py` — registry/schema agreement, every deterministic grader,
  fail-closed behaviour for a broken judge, and the no-graders → `None` rule.
- `test_supervision.py` — every policy, override-without-overwrite, agreement
  arithmetic, and `awaiting_review` gating.
- `test_runner.py` — sweeps with `run_agent` stubbed: scoring, refusal abort,
  cancellation, revision pinning, answer capping.
- `test_api.py` — every route, cross-tenant 404s, and the 402/400 split for a
  sweep that cannot be paid for.
- `test_public_api.py` — the importability contract of §6: no module-level
  sibling-app imports, an empty `__init__.py`, a pure layer free of the ORM,
  every advertised name present, and `grade_execution` persisting nothing.
