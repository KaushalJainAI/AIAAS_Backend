"""
The public surface of `eval/` — what another app imports.

Everything else in this package is either HTTP plumbing (`views`, `serializers`,
`urls`) or internal to a sweep. Import from here and the names stay put; import
`eval.runner._run_case` and you are on your own.

    from eval import api as evals

    grade   = await evals.grade_answer('Paris', [{'type': 'contains', 'value': 'paris'}])
    graded  = await evals.grade_execution(execution, specs)   # score a run that already happened
    run     = await evals.run_suite_now(suite, agent, user)   # sweep and await it
    run_id  = await evals.start_suite_run(suite, agent, user) # sweep in the background
    review  = evals.record_review(result, reviewer=user, verdict='fail')

**Why a façade module rather than `eval/__init__.py`.** `INSTALLED_APPS` makes
Django import the `eval` package itself during setup, *before* the app registry
is ready — so exporting anything that touches `eval.models` from `__init__.py`
would raise `AppRegistryNotReady` on every boot. `__init__.py` stays empty
deliberately; this module is safe to import anywhere the ORM is usable.

**Two layers, and only one of them needs the database.**

- `grade_answer` / `grade_specs` / `list_graders` and `needs_review` are pure.
  No rows, no provider (unless a spec asks for `llm_judge`), no Django models —
  usable to score anything a caller already has in hand.
- everything else operates on `eval` rows, and takes them as objects rather than
  ids, so ownership stays the caller's business to have settled.
"""
from __future__ import annotations

from typing import Any

from . import graders, queries, runner, supervision

# ── Re-exports: the stable names ─────────────────────────────────────────────
#
# Aliased at import time rather than wrapped. A wrapper here would be a second
# signature to keep in step with the real one, which is exactly the drift this
# module exists to prevent.

# Grading
Grade = graders.Grade
GradeContext = graders.GradeContext
GraderError = graders.GraderError
grade_specs = graders.grade_all
validate_graders = graders.validate_specs

# Sweeping
NoCasesToRun = runner.NoCasesToRun
open_run = runner.open_run
run_suite_now = runner.run_suite_now
start_suite_run = runner.start_suite_run

# Supervision
UNCERTAIN_BAND = supervision.UNCERTAIN_BAND
POLICIES = supervision.POLICIES
needs_review = supervision.needs_review
record_review = supervision.record_review
recompute = supervision.recompute
notify_reviewer = supervision.notify_reviewer

# Reads
review_queue = queries.review_queue
reviewable_result = queries.reviewable_result
agent_scorecard = queries.agent_scorecard
run_page = queries.run_page
run_with_results = queries.run_with_results
suite_health = queries.suite_health


def list_graders() -> list[dict[str, Any]]:
    """Every grader a case may use. Pure; safe to call at import-time in a view."""
    return graders.catalog()


async def grade_answer(answer: str, specs: list[dict[str, Any]], **context) -> dict[str, Any]:
    """Grade a piece of text against grader specs. No database, no eval rows.

    The narrow entry point for a caller who has an answer and an opinion about
    what a good one looks like — a chat turn, an extraction, a connector's
    reply. `context` is any `GradeContext` field: `reference`, `goal`,
    `tool_trace`, `structured`, `tokens`, `duration_ms`, `error`, `user_id`
    (needed only by `llm_judge`).

    Returns `{'score', 'passed', 'grades'}` — plain JSON, because a caller
    persisting this into its own table should not have to import our dataclass.
    `passed` is None when nothing could decide, and that is deliberately not
    True; see `eval/graders.py`.
    """
    grades, score, passed = await graders.grade_all(
        specs or [], graders.GradeContext(answer=answer or '', **context),
    )
    return {
        'score': score,
        'passed': passed,
        'grades': [g.as_dict() for g in grades],
    }


async def grade_execution(execution, specs: list[dict[str, Any]], *,
                          reference: str = '', **overrides) -> dict[str, Any]:
    """Grade an `ExecutionLog` that already ran, without creating a suite.

    Reads the same `output_data` keys `agents.agent.runtime` writes (`answer`,
    `tool_trace`, `structured`, `contract_error`), so a run started by anything
    — chat, a trigger, a delegation — can be scored after the fact. Useful for
    grading production traffic against a rubric rather than only fixtures.

    Nothing is persisted: the caller decides whether the verdict is worth
    keeping. A sweep is what persists, and it goes through `run_suite_now`.
    """
    payload = execution.output_data or {}
    context = {
        'structured': payload.get('structured'),
        'contract_error': payload.get('contract_error') or '',
        'tool_trace': payload.get('tool_trace') or [],
        'tokens': execution.tokens_used or 0,
        'duration_ms': execution.duration_ms or 0,
        # A run that failed is an error condition to the graders, so `no_error`
        # catches it rather than its empty answer being scored as a bad one.
        'error': execution.error_message or '',
        'goal': (execution.input_data or {}).get('goal', '') or '',
        'reference': reference,
        'user_id': execution.user_id,
    }
    context.update(overrides)
    return await grade_answer(payload.get('answer') or '', specs, **context)


__all__ = [
    # grading
    'Grade', 'GradeContext', 'GraderError', 'grade_answer', 'grade_execution',
    'grade_specs', 'list_graders', 'validate_graders',
    # sweeping
    'NoCasesToRun', 'open_run', 'run_suite_now', 'start_suite_run',
    # supervision
    'POLICIES', 'UNCERTAIN_BAND', 'needs_review', 'notify_reviewer',
    'recompute', 'record_review',
    # reads
    'agent_scorecard', 'review_queue', 'reviewable_result', 'run_page',
    'run_with_results', 'suite_health',
]
