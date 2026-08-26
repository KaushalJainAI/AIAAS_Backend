"""
Supervision tests.

Three properties carry the design and are asserted here:

1. A human verdict *overrides* the graders without *overwriting* them, so
   `grader_agreement` stays computable.
2. A run with a queued result is never reported as `completed`.
3. A case nothing could grade goes to a person under every policy but `none`.
"""
import random

from django.contrib.auth.models import User
from django.test import SimpleTestCase, TestCase

from agents.models import SubAgent
from eval import supervision
from eval.models import EvalCase, EvalResult, EvalRun, EvalSuite


class PolicyTests(SimpleTestCase):
    def test_none_queues_nothing_except_the_undecidable(self):
        queue, _ = supervision.needs_review('none', auto_passed=False, score=0.0)
        self.assertFalse(queue)

    def test_an_undecidable_case_is_queued_under_every_other_policy(self):
        for policy in ('failures', 'disagreement', 'sampled', 'all'):
            with self.subTest(policy=policy):
                queue, reason = supervision.needs_review(
                    policy, auto_passed=None, score=0.0, sample_percent=0,
                )
                self.assertTrue(queue)
                self.assertIn('no grader', reason)

    def test_failures_queues_only_failures(self):
        self.assertTrue(supervision.needs_review('failures', auto_passed=False, score=0)[0])
        self.assertFalse(supervision.needs_review('failures', auto_passed=True, score=1)[0])

    def test_all_queues_everything(self):
        self.assertTrue(supervision.needs_review('all', auto_passed=True, score=1.0)[0])

    def test_sampled_respects_its_percentage(self):
        never = supervision.needs_review(
            'sampled', auto_passed=True, score=1.0, sample_percent=0,
        )
        always = supervision.needs_review(
            'sampled', auto_passed=True, score=1.0, sample_percent=100,
        )
        self.assertFalse(never[0])
        self.assertTrue(always[0])

    def test_sampled_is_deterministic_given_a_seeded_rng(self):
        rng = random.Random(1234)
        picks = [
            supervision.needs_review(
                'sampled', auto_passed=True, score=1.0, sample_percent=50, rng=rng,
            )[0]
            for _ in range(20)
        ]
        self.assertIn(True, picks)
        self.assertIn(False, picks)

    def test_disagreement_queues_a_split_verdict(self):
        queue, reason = supervision.needs_review(
            'disagreement', auto_passed=False, score=0.5,
            grades=[{'type': 'contains', 'passed': True, 'score': 1.0},
                    {'type': 'regex', 'passed': False, 'score': 0.0}],
        )
        self.assertTrue(queue)
        self.assertIn('disagreed', reason)

    def test_disagreement_queues_an_uncertain_judge(self):
        queue, reason = supervision.needs_review(
            'disagreement', auto_passed=True, score=1.0,
            grades=[{'type': 'llm_judge', 'passed': True, 'score': 0.5}],
        )
        self.assertTrue(queue)
        self.assertIn('judge', reason)

    def test_disagreement_leaves_confident_verdicts_alone(self):
        queue, _ = supervision.needs_review(
            'disagreement', auto_passed=True, score=1.0,
            grades=[{'type': 'contains', 'passed': True, 'score': 1.0}],
        )
        self.assertFalse(queue)

    def test_an_unknown_policy_does_not_queue(self):
        # Fail quiet rather than flooding someone's queue from a typo.
        self.assertFalse(
            supervision.needs_review('whatever', auto_passed=False, score=0.0)[0]
        )


class ReviewTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user('rev', 'rev@example.com', 'pw')
        self.agent = SubAgent.objects.create(user=self.user, name='Agent')
        self.suite = EvalSuite.objects.create(
            user=self.user, name='Suite', subagent=self.agent,
            supervision='all', pass_threshold=0.5,
        )
        self.case = EvalCase.objects.create(suite=self.suite, goal='hi')
        self.run = EvalRun.objects.create(
            suite=self.suite, subagent=self.agent, user=self.user,
            status='running', supervision='all',
        )

    def result(self, **kwargs):
        defaults = dict(
            run=self.run, case=self.case, status='graded',
            auto_passed=True, auto_score=1.0, review_state='pending',
        )
        return EvalResult.objects.create(**{**defaults, **kwargs})

    def test_a_pending_result_keeps_the_run_out_of_completed(self):
        self.result()
        supervision.recompute(self.run)
        self.run.refresh_from_db()
        self.assertEqual(self.run.status, 'awaiting_review')
        # A provisional pass is not a pass.
        self.assertIsNone(self.run.passed)

    def test_a_verdict_settles_the_run(self):
        result = self.result()
        supervision.record_review(result, reviewer=self.user, verdict='pass')
        self.run.refresh_from_db()
        self.assertEqual(self.run.status, 'completed')
        self.assertTrue(self.run.passed)
        self.assertEqual(self.run.pending_review_count, 0)

    def test_a_human_overrides_without_overwriting(self):
        result = self.result(auto_passed=True, auto_score=1.0)
        supervision.record_review(result, reviewer=self.user, verdict='fail')

        result.refresh_from_db()
        self.run.refresh_from_db()
        # The graders' answer survives — it is what agreement is measured from.
        self.assertTrue(result.auto_passed)
        self.assertEqual(result.auto_score, 1.0)
        self.assertFalse(result.final_passed)
        self.assertEqual(result.final_score, 0.0)
        self.assertEqual(self.run.score, 0.0)
        self.assertFalse(self.run.passed)

    def test_agreement_is_recorded_and_aggregated(self):
        agreeing = self.result(auto_passed=True)
        disagreeing = self.result(auto_passed=True)
        supervision.record_review(agreeing, reviewer=self.user, verdict='pass')
        supervision.record_review(disagreeing, reviewer=self.user, verdict='fail')

        self.run.refresh_from_db()
        self.assertAlmostEqual(self.run.grader_agreement, 0.5)

    def test_unsure_is_recorded_but_does_not_move_the_score(self):
        result = self.result(auto_passed=True, auto_score=1.0)
        supervision.record_review(result, reviewer=self.user, verdict='unsure')

        result.refresh_from_db()
        self.run.refresh_from_db()
        self.assertIsNone(result.review.agreed_with_graders)
        self.assertTrue(result.final_passed)
        # Nothing to agree or disagree with, so agreement stays unmeasured
        # rather than being dragged down by an honest "I cannot tell".
        self.assertIsNone(self.run.grader_agreement)

    def test_a_reviewer_may_change_their_mind(self):
        result = self.result()
        supervision.record_review(result, reviewer=self.user, verdict='fail')
        supervision.record_review(result, reviewer=self.user, verdict='pass',
                                  comment='misread it')

        result.refresh_from_db()
        self.assertEqual(result.review.verdict, 'pass')
        self.assertEqual(result.review.comment, 'misread it')
        self.assertEqual(EvalResult.objects.get(pk=result.pk).review_state, 'reviewed')

    def test_an_errored_case_drags_the_score_down(self):
        self.result(status='graded', auto_passed=True, auto_score=1.0,
                    review_state='not_required')
        self.result(status='error', auto_passed=None, auto_score=0.0,
                    review_state='not_required')
        supervision.recompute(self.run)
        self.run.refresh_from_db()
        self.assertAlmostEqual(self.run.score, 0.5)
        self.assertEqual(self.run.error_count, 1)

    def test_recompute_leaves_a_cancelled_run_cancelled(self):
        self.result(review_state='not_required')
        self.run.status = 'cancelled'
        self.run.save(update_fields=['status'])
        supervision.recompute(self.run)
        self.run.refresh_from_db()
        self.assertEqual(self.run.status, 'cancelled')

    def test_notification_is_one_per_run_not_one_per_result(self):
        from notifications.models import Notification

        for _ in range(5):
            self.result()
        supervision.recompute(self.run)
        self.run.refresh_from_db()
        supervision.notify_reviewer(self.run)

        notes = Notification.objects.filter(user=self.user)
        self.assertEqual(notes.count(), 1)
        self.assertEqual(notes.first().data['kind'], 'eval_review')
        self.assertEqual(notes.first().data['pending'], 5)
