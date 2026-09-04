"""
HTTP surface tests for `/api/eval/`.

Two themes: every route is scoped to the caller (another user's suite, case,
run or result is a 404, never a 403 that confirms it exists), and a sweep that
cannot be paid for is refused while the caller is still listening.
"""
from unittest.mock import patch

from django.contrib.auth.models import User
from django.urls import reverse
from rest_framework.test import APITestCase

from agents.models import SubAgent
from eval.models import EvalCase, EvalResult, EvalRun, EvalSuite


class EvalAPITestCase(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user('owner', 'owner@example.com', 'pw')
        self.other = User.objects.create_user('other', 'other@example.com', 'pw')
        self.client.force_authenticate(self.user)

        self.agent = SubAgent.objects.create(user=self.user, name='Geo')
        self.suite = EvalSuite.objects.create(
            user=self.user, name='Capitals', subagent=self.agent, supervision='all',
        )
        self.case = EvalCase.objects.create(
            suite=self.suite, goal='Capital of France?',
            graders=[{'type': 'contains', 'value': 'paris'}],
        )


class GraderCatalogTests(EvalAPITestCase):
    def test_catalog_lists_runnable_graders(self):
        response = self.client.get(reverse('eval:grader_catalog'))
        self.assertEqual(response.status_code, 200)
        types = {g['type'] for g in response.data['graders']}
        self.assertIn('contains', types)
        self.assertIn('llm_judge', types)


class SuiteTests(EvalAPITestCase):
    def test_list_shows_only_my_suites(self):
        EvalSuite.objects.create(user=self.other, name='Theirs')
        response = self.client.get(reverse('eval:suite_list'))
        self.assertEqual(response.status_code, 200)
        self.assertEqual([s['name'] for s in response.data['suites']], ['Capitals'])

    def test_create(self):
        response = self.client.post(reverse('eval:suite_list'), {
            'name': 'Tone', 'supervision': 'sampled', 'sample_percent': 10,
        }, format='json')
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data['slug'], 'tone')

    def test_cannot_point_a_suite_at_someone_elses_agent(self):
        theirs = SubAgent.objects.create(user=self.other, name='Theirs')
        response = self.client.post(reverse('eval:suite_list'), {
            'name': 'Sneaky', 'subagent': theirs.id,
        }, format='json')
        self.assertEqual(response.status_code, 400)

    def test_unknown_supervision_policy_is_refused(self):
        response = self.client.post(reverse('eval:suite_list'), {
            'name': 'Bad', 'supervision': 'vibes',
        }, format='json')
        self.assertEqual(response.status_code, 400)

    def test_threshold_must_be_a_fraction(self):
        response = self.client.post(reverse('eval:suite_list'), {
            'name': 'Bad', 'pass_threshold': 80,
        }, format='json')
        self.assertEqual(response.status_code, 400)

    def test_concurrency_is_capped(self):
        response = self.client.post(reverse('eval:suite_list'), {
            'name': 'Greedy', 'concurrency': 99,
        }, format='json')
        self.assertEqual(response.status_code, 400)

    def test_detail_carries_the_cases(self):
        response = self.client.get(
            reverse('eval:suite_detail', args=[self.suite.id])
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.data['cases']), 1)

    def test_another_users_suite_is_a_404(self):
        theirs = EvalSuite.objects.create(user=self.other, name='Theirs')
        response = self.client.get(reverse('eval:suite_detail', args=[theirs.id]))
        self.assertEqual(response.status_code, 404)

    def test_patch_and_delete(self):
        url = reverse('eval:suite_detail', args=[self.suite.id])
        self.assertEqual(
            self.client.patch(url, {'supervision': 'none'}, format='json').status_code, 200,
        )
        self.assertEqual(self.client.delete(url).status_code, 204)
        self.assertFalse(EvalSuite.objects.filter(pk=self.suite.pk).exists())


class CaseTests(EvalAPITestCase):
    def test_create_validates_the_graders(self):
        url = reverse('eval:case_list', args=[self.suite.id])
        good = self.client.post(url, {
            'goal': 'x', 'graders': [{'type': 'regex', 'pattern': r'\d+'}],
        }, format='json')
        bad = self.client.post(url, {
            'goal': 'x', 'graders': [{'type': 'telepathy'}],
        }, format='json')
        self.assertEqual(good.status_code, 201)
        self.assertEqual(bad.status_code, 400)

    def test_a_grader_missing_its_parameter_is_refused_on_write(self):
        # The point of validating here: a case that saves is a case that runs.
        response = self.client.post(reverse('eval:case_list', args=[self.suite.id]), {
            'goal': 'x', 'graders': [{'type': 'contains'}],
        }, format='json')
        self.assertEqual(response.status_code, 400)

    def test_suite_size_is_capped(self):
        with patch('eval.views.EVAL_MAX_CASES_PER_SUITE', 1):
            response = self.client.post(
                reverse('eval:case_list', args=[self.suite.id]),
                {'goal': 'one too many'}, format='json',
            )
        self.assertEqual(response.status_code, 400)

    def test_cannot_add_a_case_to_someone_elses_suite(self):
        theirs = EvalSuite.objects.create(user=self.other, name='Theirs')
        response = self.client.post(
            reverse('eval:case_list', args=[theirs.id]), {'goal': 'x'}, format='json',
        )
        self.assertEqual(response.status_code, 404)

    def test_deleting_a_case_keeps_the_results_that_scored_it(self):
        run = EvalRun.objects.create(suite=self.suite, user=self.user, status='completed')
        result = EvalResult.objects.create(
            run=run, case=self.case, case_name='Capital of France?', status='graded',
        )
        self.client.delete(reverse('eval:case_detail', args=[self.case.id]))

        result.refresh_from_db()
        self.assertIsNone(result.case_id)
        self.assertEqual(result.case_name, 'Capital of France?')


class SweepTests(EvalAPITestCase):
    def url(self):
        return reverse('eval:suite_run', args=[self.suite.id])

    def test_accepted_returns_a_run_id(self):
        async def fake_start(suite, agent, user, notes=''):
            return 'run-123'

        with patch('eval.runner.start_suite_run', fake_start):
            response = self.client.post(self.url(), {}, format='json')
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.data['run_id'], 'run-123')

    def test_a_spent_budget_is_a_402_not_a_dead_run(self):
        from agents.agent.runtime import AgentRunRefused

        async def refuse(suite, agent, user, notes=''):
            raise AgentRunRefused('monthly spend cap reached')

        with patch('eval.runner.start_suite_run', refuse):
            response = self.client.post(self.url(), {}, format='json')
        self.assertEqual(response.status_code, 402)
        self.assertIn('spend cap', response.data['error'])

    def test_a_missing_credential_is_a_402(self):
        from llm.access import LLMNoCredential

        async def refuse(suite, agent, user, notes=''):
            raise LLMNoCredential('No openrouter key')

        with patch('eval.runner.start_suite_run', refuse):
            response = self.client.post(self.url(), {}, format='json')
        self.assertEqual(response.status_code, 402)

    def test_a_retired_model_is_a_400(self):
        from llm.access import LLMModelUnavailable

        async def refuse(suite, agent, user, notes=''):
            raise LLMModelUnavailable('that model is gone')

        with patch('eval.runner.start_suite_run', refuse):
            response = self.client.post(self.url(), {}, format='json')
        self.assertEqual(response.status_code, 400)

    def test_an_empty_suite_is_a_400(self):
        # No stub here: the real `start_suite_run` refuses before it preflights
        # anything, which is the ordering that matters — an empty suite must
        # not cost a provider round-trip to reject.
        EvalCase.objects.filter(suite=self.suite).update(is_active=False)
        response = self.client.post(self.url(), {}, format='json')
        self.assertEqual(response.status_code, 400)
        self.assertIn('no active cases', response.data['error'])

    def test_a_suite_naming_no_agent_is_a_400(self):
        self.suite.subagent = None
        self.suite.save(update_fields=['subagent'])
        response = self.client.post(self.url(), {}, format='json')
        self.assertEqual(response.status_code, 400)
        self.assertIn('names no agent', response.data['error'])

    def test_cannot_sweep_with_someone_elses_agent(self):
        theirs = SubAgent.objects.create(user=self.other, name='Theirs')
        response = self.client.post(self.url(), {'agent_id': theirs.id}, format='json')
        self.assertEqual(response.status_code, 404)


class RunFixture(EvalAPITestCase):
    """A sweep with one failed, queued result. Shared by the three read
    surfaces below; a mixin rather than a base test class so its cases are not
    re-run once per subclass."""

    def setUp(self):
        super().setUp()
        self.run = EvalRun.objects.create(
            suite=self.suite, subagent=self.agent, user=self.user,
            status='awaiting_review', score=0.5, pending_review_count=1,
        )
        self.result = EvalResult.objects.create(
            run=self.run, case=self.case, case_name='Capital of France?',
            status='graded', auto_passed=False, auto_score=0.0,
            grades=[{'type': 'contains', 'passed': False, 'score': 0.0,
                     'weight': 1.0, 'detail': "missing 'paris'"}],
            review_state='pending', review_reason='the graders failed this case',
        )


class RunReadTests(RunFixture):
    def test_run_list_filters_by_suite(self):
        response = self.client.get(reverse('eval:run_list'), {'suite_id': self.suite.id})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['count'], 1)

    def test_run_list_rejects_an_unknown_status(self):
        response = self.client.get(reverse('eval:run_list'), {'status': 'nonsense'})
        self.assertEqual(response.status_code, 400)

    def test_run_detail_carries_results_and_grades(self):
        response = self.client.get(
            reverse('eval:run_detail', args=[str(self.run.run_id)])
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['result_count'], 1)
        self.assertEqual(response.data['results'][0]['grades'][0]['type'], 'contains')

    def test_a_malformed_run_id_is_a_404_not_a_500(self):
        response = self.client.get(reverse('eval:run_detail', args=['not-a-uuid']))
        self.assertEqual(response.status_code, 404)

    def test_another_users_run_is_a_404(self):
        theirs = EvalRun.objects.create(
            suite=EvalSuite.objects.create(user=self.other, name='T'), user=self.other,
        )
        response = self.client.get(reverse('eval:run_detail', args=[str(theirs.run_id)]))
        self.assertEqual(response.status_code, 404)

    def test_cancel(self):
        response = self.client.post(
            reverse('eval:run_cancel', args=[str(self.run.run_id)])
        )
        self.assertEqual(response.status_code, 200)
        self.run.refresh_from_db()
        self.assertEqual(self.run.status, 'cancelled')

    def test_cancelling_a_finished_run_is_a_400(self):
        self.run.status = 'completed'
        self.run.save(update_fields=['status'])
        response = self.client.post(
            reverse('eval:run_cancel', args=[str(self.run.run_id)])
        )
        self.assertEqual(response.status_code, 400)


class ReviewTests(RunFixture):
    def test_queue_shows_what_is_waiting_on_me(self):
        response = self.client.get(reverse('eval:review_queue'))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['count'], 1)
        entry = response.data['queue'][0]
        self.assertEqual(entry['review_reason'], 'the graders failed this case')
        self.assertEqual(entry['suite_name'], 'Capitals')

    def test_queue_does_not_leak_another_users_results(self):
        self.client.force_authenticate(self.other)
        response = self.client.get(reverse('eval:review_queue'))
        self.assertEqual(response.data['count'], 0)

    def test_a_verdict_settles_the_run(self):
        response = self.client.post(
            reverse('eval:submit_review', args=[self.result.id]),
            {'verdict': 'pass', 'comment': 'Berlin is wrong but the reasoning is right'},
            format='json',
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['run']['status'], 'completed')
        # The graders said fail, the human said pass: a disagreement, recorded.
        self.assertIs(response.data['agreed_with_graders'], False)
        self.assertEqual(response.data['run']['grader_agreement'], 0.0)

    def test_an_unknown_verdict_is_refused(self):
        response = self.client.post(
            reverse('eval:submit_review', args=[self.result.id]),
            {'verdict': 'maybe'}, format='json',
        )
        self.assertEqual(response.status_code, 400)

    def test_cannot_review_someone_elses_result(self):
        self.client.force_authenticate(self.other)
        response = self.client.post(
            reverse('eval:submit_review', args=[self.result.id]),
            {'verdict': 'pass'}, format='json',
        )
        self.assertEqual(response.status_code, 404)

    def test_an_errored_case_cannot_be_reviewed(self):
        # There is no answer to have an opinion about, and letting one in would
        # dilute the agreement figure with verdicts on outages.
        self.result.status = 'error'
        self.result.save(update_fields=['status'])
        response = self.client.post(
            reverse('eval:submit_review', args=[self.result.id]),
            {'verdict': 'fail'}, format='json',
        )
        self.assertEqual(response.status_code, 400)


class ScorecardTests(RunFixture):
    def test_scorecard_groups_by_suite(self):
        response = self.client.get(
            reverse('eval:agent_scorecard', args=[self.agent.id])
        )
        self.assertEqual(response.status_code, 200)
        entry = response.data['suites'][0]
        self.assertEqual(entry['suite_name'], 'Capitals')
        # Still awaiting review, so there is no settled score to report.
        self.assertIsNone(entry['latest'])
        self.assertEqual(entry['awaiting_review'], 1)

    def test_another_users_agent_is_a_404(self):
        theirs = SubAgent.objects.create(user=self.other, name='Theirs')
        response = self.client.get(reverse('eval:agent_scorecard', args=[theirs.id]))
        self.assertEqual(response.status_code, 404)
