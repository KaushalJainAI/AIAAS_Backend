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
baseline_for = queries.baseline_for


def starter_kit_list() -> list[dict]:
    """Starter kit cards for a picker. Pure; no rows."""
    from . import starter_kits as _kits
    return [
        {'slug': slug, 'name': kit['name'],
         'description': kit.get('description', ''),
         'case_count': len(kit.get('cases', []))}
        for slug, kit in _kits.STARTER_KITS.items()
    ]


def recommended_kits_for(tool_grants: dict | None) -> list[str]:
    """Which starter kits fit an agent's grants. Pure."""
    from . import starter_kits as _kits
    return _kits.recommended_kits(tool_grants or {})


def clone_starter_kit(*, user, template: str, name: str = '',
                      agent=None):
    """Clone a starter kit into a suite + cases owned by `user`. Sync ORM.

    Used by the from-template view and by the orchestrator tools. Raises
    `GraderError` if a kit case drifted from the registry instead of
    installing a suite that can never pass.
    """
    from asgiref.sync import sync_to_async  # noqa: F401  (kept local on purpose)

    from . import starter_kits as _kits
    from .models import EvalCase, EvalSuite

    kit = _kits.get_kit(template)
    if kit is None:
        raise GraderError(f'No such starter kit {template!r}.')
    suite = EvalSuite.objects.create(
        user=user,
        name=(name or kit['name'])[:200],
        description=kit.get('description', ''),
        subagent=agent,
        supervision='disagreement',
        template_slug=str(template).strip().lower(),
    )
    rows = []
    for i, case_def in enumerate(kit['cases']):
        validated = graders.validate_case_graders(case_def.get('graders', []))
        rows.append(EvalCase(
            suite=suite, order=i,
            name=str(case_def.get('name', f'Case {i + 1}'))[:200],
            goal=str(case_def.get('goal', '')),
            input_data=dict(case_def.get('input_data', {}) or {}),
            reference=str(case_def.get('reference', '')),
            graders=validated,
            tags=['starter', str(template).strip().lower()],
        ))
    EvalCase.objects.bulk_create(rows)
    return suite


def save_cases(suite, cases: list[dict[str, Any]], *, drafts: bool = True,
               world_version: int | None = None):
    """The one write path for model-derived cases. Sync ORM.

    Generation, run imports, "save as case", the chat `add_eval_case` tool
    and the `/eval` command all arrive here, so the draft rule lives in one
    place: `drafts=True` saves `is_active=False` tagged `needs-review`, and
    the runner sweeps only active cases — nothing a model wrote counts
    towards a score until a person accepts it on the Evals page. The
    hand-written case editor (`case_list` POST) is the only other writer,
    and it stays separate: a person wrote those.

    Each case: `{name?, goal, input_data?, reference?, graders?, tags?}`.
    Graders are validated through the same registry the runner dispatches
    through (`GraderError` on unknown). Orders continue past the suite's
    existing cases; every row is stamped with the live world version (or
    the override a fresh world draft passes), so generation-time cases
    belong to the world they were built for. The suite cap bounds the
    write, never silently truncates it: rows past the room are not created,
    and the caller reports how many landed.
    """
    from django.db.models import Max

    from workflow_backend.thresholds import EVAL_MAX_CASES_PER_SUITE

    from .environment import case_world_version
    from .generator import DRAFT_TAG
    from .models import EvalCase

    room = max(0, EVAL_MAX_CASES_PER_SUITE - suite.cases.count())
    start = (suite.cases.aggregate(m=Max('order'))['m'] or 0) + 1
    if world_version is None:
        world_version = case_world_version(suite)
    rows = []
    for offset, draft in enumerate(cases[:room]):
        validated = graders.validate_case_graders(draft.get('graders') or [])
        tags = [str(t) for t in (draft.get('tags') or []) if str(t).strip()]
        if drafts:
            tags = [t for t in tags if t != DRAFT_TAG] + [DRAFT_TAG]
        rows.append(EvalCase(
            suite=suite, order=start + offset,
            name=str(draft.get('name') or draft.get('goal', '')[:60])[:200],
            goal=str(draft.get('goal') or ''),
            input_data=dict(draft.get('input_data') or {}),
            reference=str(draft.get('reference') or ''),
            graders=validated, tags=tags,
            is_active=not drafts, world_version=world_version,
        ))
    EvalCase.objects.bulk_create(rows)
    return rows


def save_generated_world(suite, out: dict[str, Any]):
    """Persist a `generate_world` result as a draft world + draft cases.

    Shared by the HTTP view and the chat tool, so both mint versions the
    same way: the next version number, `draft` status, and case drafts on
    that version through `save_cases` (which stamps and validates them).
    Sync ORM.
    """
    from django.db.models import Max

    from .models import EvalWorld

    version = (suite.worlds.aggregate(m=Max('version'))['m'] or 0) + 1
    world = EvalWorld.objects.create(
        suite=suite, version=version, status='draft',
        brief=out.get('brief', ''), surfaces=out.get('surfaces') or {},
        fixtures=out.get('fixtures') or {}, facts=out.get('facts') or [],
        created_by_model=out.get('model') or '',
        cost_usd=out.get('cost_usd'),
    )
    tagged = []
    for case in out.get('cases') or []:
        entry = dict(case)
        tags = ['generated', 'needs-review', case.get('category', '')]
        entry['tags'] = [t for t in tags if t]
        tagged.append(entry)
    saved = save_cases(suite, tagged, drafts=True, world_version=world.version)
    return world, saved


def list_graders() -> list[dict[str, Any]]:
    """Every grader a case may use. Pure; safe to call at import-time in a view."""
    return graders.catalog()


async def grade_answer(answer: str, specs: list[dict[str, Any]], **context) -> dict[str, Any]:
    """Grade a piece of text against grader specs. No database, no eval rows.

    The narrow entry point for a caller who has an answer and an opinion about
    what a good one looks like — a chat turn, an extraction, a connector's
    reply. `context` is any `GradeContext` field: `reference`, `goal`,
    `tool_trace`, `structured`, `tokens`, `duration_ms`, `error`, `user_id`
    (needed only by `llm_judge`), `files`, `binaries`, `env`.

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
        'awaiting_approval': execution.status == 'paused',
        'goal': (execution.input_data or {}).get('goal', '') or '',
        'reference': reference,
        'user_id': execution.user_id,
        'reasoning': await _run_reasoning(execution),
    }
    context.update(overrides)
    return await grade_answer(payload.get('answer') or '', specs, **context)


async def _run_reasoning(execution) -> str:
    """The run's per-turn reasoning, in turn order, for the judge.

    Read from `AgentTurn` rather than `output_data`: the turn row is where the
    runtime records reasoning in full (`logs/models.py`), and a run graded after
    the fact has nothing else to show it.
    """
    from asgiref.sync import sync_to_async

    def read() -> str:
        rows = execution.turns.order_by('index').values_list('index', 'reasoning')
        return '\n\n'.join(f'[turn {i}] {text}' for i, text in rows if text)

    return await sync_to_async(read)()


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
    'run_with_results', 'suite_health', 'baseline_for',
    # writes (model-derived cases + generated worlds; drafts by default)
    'save_cases', 'save_generated_world',
    # starter kits (user datasets + orchestrator)
    'starter_kit_list', 'recommended_kits_for', 'clone_starter_kit',
]
