# `eval/`: is this agent any good?

Tests agents the way unit tests test code. A **suite** holds **cases** (an
input and what a good answer looks like). A **sweep** runs the agent on every
case and **graders** score each answer. A person can review what the graders
decided, and the app measures how often the graders were right.

Design: [`docs/EVALUATION.md`](../docs/EVALUATION.md).
The practical benchmark lives in [`benchmarks/`](benchmarks/README.md).

> The Django app label is `eval` (singular). An older `evals` app left unused
> `evals_*` tables in some dev databases; ignore them.

## Data (`models.py`)

| Model | What it is |
|---|---|
| `EvalSuite`, `EvalCase` | A test suite and its cases |
| `EvalRun` | One sweep of a suite against an agent |
| `EvalResult` | One case's outcome. `auto_passed` is what the graders said, kept forever |
| `EvalReview` | A person's verdict on a result. The score uses this when it exists |
| `JudgeCalibration` | How well the AI judge agrees with known-correct labels |

## Files

| File | What it does |
|---|---|
| `api.py` | **What other apps import.** Use this, not the internals |
| `graders.py` | The graders: small named checks (`contains`, `json_value`, `llm_judge`...). Adding one = registering it here |
| `runner.py` | Running a suite: one agent run per case, grade, queue doubtful ones for review |
| `supervision.py` | When a result needs a person, and how the final score is computed |
| `queries.py` | Database reads behind `/api/eval/` |
| `views.py`, `urls.py`, `serializers.py` | `/api/eval/` |
| `workspace.py` | Per-case folders of starting files, reset before each attempt |
| `office_files.py` | Reading `.xlsx` / `.docx` / `.pptx` back for grading |
| `calibration.py` | Checking the AI judge |
| `bare.py` | Running the plain model with no platform, for comparison |
| `generator.py`, `starter_kits.py` | Test data a user didn't have to write |
| `recovery.py` | Closing sweeps a restart interrupted |
| `benchmarks/` | The built-in benchmark suites, as code |

## Key rules

- Eval runs use `caller='eval'`, so they don't count against spend caps or show up on the Activity page.
- A result with no graders is *not* a pass. It goes to a person.
- The judge model and the model under test must be different
  (see "Model IDs" in `CLAUDE.md`).

## Management commands

- `benchmark run --user <email>`: run the benchmark and write a scorecard.
- `seed_starter_evals`: starter suites.

## Tests

`eval/tests/`.
