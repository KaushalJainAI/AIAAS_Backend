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
    existing cases. Only a world generation passes `world_version`: those
    cases were built for that world and run inside it. Everything else here
    — run imports, "save as case", `add_eval_case`, `/eval`, config-only
    drafts — is about the agent's real situation, not the world, so it is
    saved with no version and runs outside the world (stamping it would
    confine a "summarise my Q3 report" case to fake files, and make it go
    stale the moment the world is regenerated). The suite cap bounds the
    write, never silently truncates it: rows past the room are not created,
    and the caller reports how many landed.
    """
    from django.db.models import Max

    from workflow_backend.thresholds import EVAL_MAX_CASES_PER_SUITE

    from .generator import DRAFT_TAG
    from .models import EvalCase

    room = max(0, EVAL_MAX_CASES_PER_SUITE - suite.cases.count())
    start = (suite.cases.aggregate(m=Max('order'))['m'] or 0) + 1
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


class WorldGenerationBusy(Exception):
    """A world for this suite is already being generated."""


#: A `generating` row older than this belongs to a process that died; the
#: recovery sweep marks it `failed`. Generation is ~5 judge calls, so this
#: is generous rather than tight.
WORLD_GENERATION_STALE_MINUTES = 30


def _mint_world(suite, **fields):
    """The next world version for `suite`. Sync ORM."""
    from django.db.models import Max

    from .models import EvalWorld

    version = (suite.worlds.aggregate(m=Max('version'))['m'] or 0) + 1
    return EvalWorld.objects.create(suite=suite, version=version, **fields)


def save_generated_world(suite, out: dict[str, Any]):
    """Persist a `generate_world` result as a new draft world + draft cases.

    Kept for callers holding a finished result (tests, scripts); the HTTP
    view and the chat tool go through `start_world_generation`, which mints
    the row first and fills it in the background. Sync ORM.
    """
    world = _mint_world(suite, status='generating')
    return fill_generated_world(world, out)


def fill_generated_world(world, out: dict[str, Any]):
    """Write a finished generation into its row: fixtures, facts, and the
    case drafts on that version (through `save_cases`, which validates
    them). The row becomes `draft`. Sync ORM."""
    world.status = 'draft'
    world.error_message = ''
    world.brief = out.get('brief', '')
    world.surfaces = out.get('surfaces') or {}
    world.fixtures = out.get('fixtures') or {}
    world.facts = out.get('facts') or []
    world.created_by_model = out.get('model') or ''
    world.cost_usd = out.get('cost_usd')
    world.rejected = list(out.get('rejected') or [])[:100]
    world.save()
    suite = world.suite
    tagged = []
    for case in out.get('cases') or []:
        entry = dict(case)
        tags = ['generated', 'needs-review', case.get('category', '')]
        entry['tags'] = [t for t in tags if t]
        tagged.append(entry)
    saved = save_cases(suite, tagged, drafts=True, world_version=world.version)
    return world, saved


async def start_world_generation(suite, user, *, focus: str = '', cases: int = 12):
    """Mint a `generating` world and build it in the background.

    Five or so reasoning-model calls outlast an HTTP request (the frontend
    gives up at five minutes), and a request dropped mid-generation paid for
    the calls and kept nothing. So only what can fail fast happens here —
    the agent has something a world can hold, the judge is payable, no
    generation is already running — and the rest is a detached task that
    leaves the row `draft` or `failed` and tells the owner either way.

    Raises `generator.WorldNotPossible` (a 400 about the agent),
    `llm.access.LLMUserActionable` (402, no judge credential) and
    `WorldGenerationBusy` (409). Returns the `generating` row.
    """
    from asgiref.sync import sync_to_async
    from django.conf import settings

    from llm import access as llm
    from workflow_backend.background import spawn

    from .generator import (
        DEFAULT_GENERATED, MAX_GENERATED, WorldNotPossible,
        connector_slugs_in_scope, surfaces_for_agent,
    )

    agent = suite.subagent
    scoped = await sync_to_async(connector_slugs_in_scope)(agent)
    if not any(v is True for v in surfaces_for_agent(agent, scoped).values()):
        raise WorldNotPossible(
            'This agent has nothing a generated world can hold: give it file '
            'access, a knowledge base, web search or a Google connector '
            '(Gmail, Calendar, Drive) first.')
    await llm.preflight(
        provider=getattr(settings, 'EVAL_JUDGE_PROVIDER', 'openrouter'),
        model=getattr(settings, 'EVAL_JUDGE_MODEL', ''), user_id=user.id)

    count = max(1, min(int(cases or DEFAULT_GENERATED), MAX_GENERATED))

    def mint():
        from django.db import transaction

        with transaction.atomic():
            if suite.worlds.filter(status='generating').exists():
                raise WorldGenerationBusy(
                    'A world for this suite is already being generated.')
            return _mint_world(suite, status='generating',
                               focus=(focus or '')[:500], requested_cases=count)

    world = await sync_to_async(mint)()
    spawn(_generate_into(world.id, user.id), name=f'eval-world-{world.id}')
    return world


async def _generate_into(world_id: int, user_id: int) -> None:
    """The background half of `start_world_generation`. Never raises."""
    import logging

    from asgiref.sync import sync_to_async

    from .generator import generate_world
    from .models import EvalWorld

    log = logging.getLogger(__name__)
    world = await EvalWorld.objects.select_related('suite__subagent').filter(
        id=world_id).afirst()
    if world is None:
        return
    suite = world.suite
    try:
        out = await generate_world(suite.subagent, user_id=user_id,
                                   focus=world.focus, cases=world.requested_cases)
        _, saved = await sync_to_async(fill_generated_world)(world, out)
        title = f'Test world ready for "{suite.name}"'
        message = (f'{len(saved)} draft case(s) are waiting for review on the '
                   f'Evals page. Nothing scores until you accept them.')
    except Exception as exc:  # noqa: BLE001 - recorded on the row, never lost
        log.warning('[Eval] world %s generation failed: %s', world_id, exc)
        world.status = 'failed'
        world.error_message = str(exc)[:2000] or exc.__class__.__name__
        await sync_to_async(world.save)(update_fields=['status', 'error_message', 'updated_at'])
        title = f'Test world for "{suite.name}" failed'
        message = world.error_message[:500]
    await _notify_owner(user_id, title, message)


async def _notify_owner(user_id: int, title: str, message: str) -> None:
    """One notification pointing at `/evals`. Best effort."""
    from asgiref.sync import sync_to_async

    def work():
        from django.contrib.auth import get_user_model

        from notifications.utils import create_notification

        user = get_user_model().objects.filter(id=user_id).first()
        if user is not None:
            create_notification(user, 'agent_update', title, message,
                                data={'action_url': '/evals'}, send_email=False)

    try:
        await sync_to_async(work)()
    except Exception:  # noqa: BLE001
        import logging
        logging.getLogger(__name__).warning('[Eval] world notification failed',
                                            exc_info=True)


def fail_stale_world_generations(now=None) -> int:
    """Mark `generating` rows whose process died as `failed`. Sync ORM;
    called by `eval/recovery.py` alongside the orphaned-sweep check."""
    from datetime import timedelta

    from django.utils import timezone

    from .models import EvalWorld

    cutoff = (now or timezone.now()) - timedelta(minutes=WORLD_GENERATION_STALE_MINUTES)
    return EvalWorld.objects.filter(status='generating', updated_at__lt=cutoff).update(
        status='failed',
        error_message='Generation was interrupted (the server restarted). Try again.',
        updated_at=timezone.now())


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
    'save_cases', 'save_generated_world', 'fill_generated_world',
    'start_world_generation', 'WorldGenerationBusy', 'fail_stale_world_generations',
    # starter kits (user datasets + orchestrator)
    'starter_kit_list', 'recommended_kits_for', 'clone_starter_kit',
]
