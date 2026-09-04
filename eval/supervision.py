"""
Who checks the checker.

Automatic graders are cheap and confident, and confidence is exactly the wrong
property in the cases where they are wrong. Supervision is the answer to that:
a policy that decides which results a person is asked to look at, a place for
their verdict, and — the part that makes it more than a chore — a running
measure of how often the graders and the person agree.

**Human verdicts override, they do not overwrite.** `EvalResult.auto_passed`
keeps the graders' answer for ever; `EvalReview.verdict` is what the run's score
is computed from. Two columns rather than one because `grader_agreement` is the
only number that tells you whether the suite is measuring anything, and it
cannot be computed from a field that was overwritten.

**A run whose score can still move is not `completed`.** It sits in
`awaiting_review` until every queued result has a verdict, so nothing downstream
can report a provisional score as a final one.

**A case nothing could grade does not pass.** `auto_passed is None` (a case with
no graders) is queued for a person under every policy but `none`, and scores
zero if nobody looks. Vacuous truth is how an empty suite reports 100%.
"""
from __future__ import annotations

import logging
import random

from django.db import transaction
from django.utils import timezone

logger = logging.getLogger(__name__)

#: Scores in this band are where the graders were least sure, and where a
#: reviewer's time is worth most. Used by the `disagreement` policy.
UNCERTAIN_BAND = (0.35, 0.65)

POLICIES = ('none', 'failures', 'disagreement', 'sampled', 'all')


def needs_review(policy: str, *, auto_passed, score: float,
                 grades: list[dict] | None = None,
                 sample_percent: int = 20,
                 rng: random.Random | None = None) -> tuple[bool, str]:
    """Should a person look at this result, and why."""
    if policy == 'none':
        return False, ''

    # Under every other policy, a result no grader could settle is precisely
    # the one worth a human minute — whatever else the policy says.
    if auto_passed is None:
        return True, 'no grader could decide this case'

    if policy == 'all':
        return True, 'suite reviews everything'

    if policy == 'failures':
        return (not auto_passed), 'the graders failed this case' if not auto_passed else ''

    if policy == 'sampled':
        roll = (rng or random).random() * 100
        picked = roll < max(0, min(100, int(sample_percent or 0)))
        return picked, 'sampled for review' if picked else ''

    if policy == 'disagreement':
        entries = grades or []
        verdicts = {bool(g.get('passed')) for g in entries}
        if len(verdicts) > 1:
            return True, 'graders disagreed with each other'
        for g in entries:
            if g.get('type') == 'llm_judge':
                judged = float(g.get('score') or 0.0)
                if UNCERTAIN_BAND[0] <= judged <= UNCERTAIN_BAND[1]:
                    return True, 'the judge was uncertain'
        if UNCERTAIN_BAND[0] <= float(score or 0.0) <= UNCERTAIN_BAND[1]:
            return True, 'the score was borderline'
        return False, ''

    logger.warning('[Eval] unknown supervision policy %r; not queueing', policy)
    return False, ''


def apply_policy(result, suite, *, rng: random.Random | None = None) -> None:
    """Set `review_state` on a freshly graded result. Does not save."""
    queue, reason = needs_review(
        result.run.supervision,
        auto_passed=result.auto_passed,
        score=result.auto_score,
        grades=result.grades,
        sample_percent=suite.sample_percent,
        rng=rng,
    )
    result.review_state = 'pending' if queue else 'not_required'
    result.review_reason = reason[:120]


def _agreement(auto_passed, verdict: str):
    """Did the person and the graders reach the same conclusion?

    None when there is nothing to compare — an `unsure` verdict, or a case no
    grader could settle. Counting either as a disagreement would make
    `grader_agreement` fall for reasons that are not the graders' fault.
    """
    if verdict == 'unsure' or auto_passed is None:
        return None
    return auto_passed == (verdict == 'pass')


@transaction.atomic
def record_review(result, *, reviewer, verdict: str, comment: str = '',
                  corrected_answer: str = ''):
    """Store a verdict and re-settle the run it belongs to.

    `update_or_create` rather than `create`: a reviewer changing their mind is
    an edit, not a second opinion, and the `OneToOne` would refuse the insert
    anyway — as a 500 rather than as the correction it is.
    """
    from .models import EvalReview

    review, _ = EvalReview.objects.update_or_create(
        result=result,
        defaults={
            'reviewer': reviewer,
            'verdict': verdict,
            'agreed_with_graders': _agreement(result.auto_passed, verdict),
            'comment': comment or '',
            'corrected_answer': corrected_answer or '',
        },
    )
    result.review_state = 'reviewed'
    result.save(update_fields=['review_state', 'updated_at'])
    recompute(result.run)
    return review


@transaction.atomic
def recompute(run) -> None:
    """Recount, rescore and re-status a run from its results.

    Called both when a sweep finishes and after every review, from one place
    rather than two: a score computed twice by two functions is a score that
    eventually disagrees with itself.
    """
    results = list(run.results.select_related('review').all())

    passed = failed = errored = pending = 0
    weighted_score = weighted_total = 0.0
    agree_yes = agree_total = 0

    for r in results:
        if r.status == 'error':
            errored += 1
        if r.review_state == 'pending':
            pending += 1

        review = getattr(r, 'review', None)
        if review is not None and review.agreed_with_graders is not None:
            agree_total += 1
            agree_yes += int(review.agreed_with_graders)

        if r.status in ('skipped',):
            continue

        weight = float(r.weight or 1.0)
        weighted_total += weight
        # `final_score` is 0.0 for an errored run and for one nothing could
        # grade, so both drag the score down rather than quietly vanishing
        # from the denominator.
        weighted_score += float(r.final_score or 0.0) * weight

        verdict = r.final_passed
        if verdict is True:
            passed += 1
        elif r.status != 'error':
            failed += 1

    run.passed_count = passed
    run.failed_count = failed
    run.error_count = errored
    run.pending_review_count = pending
    run.total_cases = len(results)
    run.score = (weighted_score / weighted_total) if weighted_total else None
    run.grader_agreement = (agree_yes / agree_total) if agree_total else None

    if run.status in ('failed', 'cancelled'):
        fields = ['passed_count', 'failed_count', 'error_count',
                  'pending_review_count', 'total_cases', 'score',
                  'grader_agreement', 'updated_at']
        run.save(update_fields=fields)
        return

    if pending:
        run.status = 'awaiting_review'
        run.passed = None
    else:
        run.status = 'completed'
        run.passed = (run.score or 0.0) >= float(run.suite.pass_threshold or 0.0)
        if run.completed_at is None:
            run.completed_at = timezone.now()
            if run.started_at:
                run.duration_ms = int(
                    (run.completed_at - run.started_at).total_seconds() * 1000
                )

    run.save(update_fields=[
        'status', 'passed', 'score', 'grader_agreement', 'passed_count',
        'failed_count', 'error_count', 'pending_review_count', 'total_cases',
        'completed_at', 'duration_ms', 'updated_at',
    ])


def notify_reviewer(run) -> None:
    """Tell the reviewer a sweep is waiting on them.

    One notification per run, never one per result: a 200-case suite under
    `all` would otherwise deliver 200 notifications for a single decision the
    person makes in one sitting — the same reasoning as the HITL digest in
    `notifications/reminders.py`.
    """
    if not run.pending_review_count:
        return

    from django.contrib.auth import get_user_model
    from notifications.utils import create_notification

    suite = run.suite
    reviewer_id = suite.reviewer_or_owner_id
    reviewer = get_user_model().objects.filter(id=reviewer_id).first()
    if reviewer is None:
        logger.warning('[Eval] run %s has no reviewer to notify', run.run_id)
        return

    agent = run.subagent.name if run.subagent else 'an agent'
    create_notification(
        reviewer,
        type='system',
        title=f'{run.pending_review_count} eval result(s) need review',
        message=(
            f'"{suite.name}" finished against {agent} and left '
            f'{run.pending_review_count} of {run.total_cases} result(s) for you '
            f'to settle.'
        ),
        data={
            'kind': 'eval_review',
            'run_id': str(run.run_id),
            'suite_id': suite.id,
            'pending': run.pending_review_count,
        },
    )
