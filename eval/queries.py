"""
Every read behind `/api/eval/`, so the views stay thin — the same split
`logs/queries.py` uses, and for the same reason: an aggregate assembled inside a
view is one nothing else can reuse and no test can call directly.

Everything here takes the `user` and filters on it. Ownership is not something
the caller is trusted to have checked.
"""
from __future__ import annotations

from django.core.exceptions import ValidationError
from django.db.models import Count, Q

from workflow_backend.thresholds import (
    EVAL_RESULT_LIST_LIMIT,
    EVAL_REVIEW_QUEUE_LIMIT,
)

from .models import EvalResult, EvalRun, EvalSuite


def suites_for(user):
    return EvalSuite.objects.filter(user=user).select_related('subagent')


def runs_for(user):
    return EvalRun.objects.filter(user=user).select_related(
        'suite', 'subagent', 'revision',
    )


def run_page(user, *, limit: int = 20, suite_id=None, agent_id=None, status=None):
    """One page of sweeps, newest first."""
    qs = runs_for(user)
    if suite_id:
        qs = qs.filter(suite_id=suite_id)
    if agent_id:
        qs = qs.filter(subagent_id=agent_id)
    if status:
        qs = qs.filter(status=status)

    total = qs.count()
    rows = list(qs.order_by('-created_at', '-id')[:limit])
    return rows, {'count': total, 'truncated': total > len(rows)}


def run_with_results(user, run_id: str):
    """One sweep and its results, capped.

    Returns `(run, results, meta)` or `(None, [], {})`. A malformed UUID is a
    miss rather than a 500 — the same treatment `/api/logs/executions/{id}/`
    gives it.
    """
    try:
        run = runs_for(user).filter(run_id=run_id).first()
    except (ValueError, ValidationError):
        # A malformed UUID reaches the ORM as a ValidationError. Treated as a
        # miss, so a bad id in a URL is a 404 rather than a 500.
        return None, [], {}
    if run is None:
        return None, [], {}

    qs = run.results.select_related('review', 'execution', 'case').order_by('id')
    total = qs.count()
    results = list(qs[:EVAL_RESULT_LIST_LIMIT])
    return run, results, {
        'result_count': total,
        'results_truncated': total > len(results),
    }


def review_queue(user, *, limit: int = 25, suite_id=None, run_id=None):
    """Everything waiting on this person, oldest first.

    **Oldest first**, unlike every other list here: a queue is worked through,
    and newest-first would bury the results that have been waiting longest —
    which are exactly the ones holding a run in `awaiting_review`.
    """
    limit = min(limit, EVAL_REVIEW_QUEUE_LIMIT)
    qs = EvalResult.objects.filter(
        review_state='pending',
    ).filter(
        # A suite's reviewer, or its owner when no reviewer is named. One query
        # rather than two so the queue is orderable and countable as a whole.
        Q(run__suite__reviewer=user) | Q(run__suite__reviewer__isnull=True, run__user=user)
    ).select_related('run', 'run__suite', 'run__subagent', 'case')

    if suite_id:
        qs = qs.filter(run__suite_id=suite_id)
    if run_id:
        try:
            qs = qs.filter(run__run_id=run_id)
        except ValidationError:
            return [], {'count': 0, 'truncated': False}

    total = qs.count()
    rows = list(qs.order_by('created_at', 'id')[:limit])
    return rows, {'count': total, 'truncated': total > len(rows)}


def reviewable_result(user, result_id: int):
    """One result this user is entitled to review, or None."""
    return EvalResult.objects.filter(pk=result_id).filter(
        Q(run__suite__reviewer=user) | Q(run__suite__reviewer__isnull=True, run__user=user)
    ).select_related('run', 'run__suite', 'review').first()


def agent_scorecard(user, agent_id: int, *, history: int = 10):
    """How one agent scores across every suite pointed at it.

    The shape a "should I ship this change?" screen needs: per suite, the
    latest settled score, the revision it was scored under, and the previous
    scores to compare against. Runs still `awaiting_review` are reported but
    kept out of `latest` — a provisional score presented as the current one is
    how an unreviewed sweep silently becomes a release decision.
    """
    runs = runs_for(user).filter(subagent_id=agent_id).order_by('-created_at')[:200]

    by_suite: dict[int, dict] = {}
    for run in runs:
        entry = by_suite.setdefault(run.suite_id, {
            'suite_id': run.suite_id,
            'suite_name': run.suite.name,
            'latest': None,
            'awaiting_review': 0,
            'history': [],
        })
        point = {
            'run_id': str(run.run_id),
            'score': run.score,
            'passed': run.passed,
            'status': run.status,
            'revision': run.revision.number if run.revision_id else None,
            'grader_agreement': run.grader_agreement,
            'created_at': run.created_at,
        }
        if run.status == 'awaiting_review':
            entry['awaiting_review'] += 1
        if entry['latest'] is None and run.status == 'completed':
            entry['latest'] = point
        if len(entry['history']) < history:
            entry['history'].append(point)

    return sorted(by_suite.values(), key=lambda e: e['suite_name'].lower())


def suite_health(user):
    """Per-suite counts a dashboard reads: cases, runs, and what is queued."""
    return list(
        suites_for(user)
        .annotate(
            case_count=Count('cases', filter=Q(cases__is_active=True), distinct=True),
            run_count=Count('runs', distinct=True),
            pending_reviews=Count(
                'runs__results',
                filter=Q(runs__results__review_state='pending'),
                distinct=True,
            ),
        )
        .values('id', 'name', 'supervision', 'case_count', 'run_count', 'pending_reviews')
    )
