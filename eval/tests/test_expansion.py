"""
Expansion plan tests: new graders, flags, starter kits, wizard (read-only).

Every case here is deterministic — no provider, no network — so the suite
stays free to run on every commit.
"""
from asgiref.sync import async_to_sync
from django.contrib.auth.models import User
from django.test import SimpleTestCase, TestCase
from rest_framework.test import APITestCase

from eval import graders
from eval.graders import GradeContext


def grade(specs, **ctx):
    return async_to_sync(graders.grade_all)(specs, GradeContext(**ctx))


class NewGraderTests(SimpleTestCase):
    def test_disallowed_denylist(self):
        _, _, passed = grade(
            [{'type': 'disallowed_tool_used', 'tools': ['gmail_send_message']}],
            answer='hi', tool_trace=[{'tool': 'gmail_send_message', 'args': {}}])
        self.assertFalse(passed)
        _, _, passed = grade(
            [{'type': 'disallowed_tool_used', 'tools': ['gmail_send_message']}],
            answer='hi', tool_trace=[{'tool': 'web_search', 'args': {}}])
        self.assertTrue(passed)

    def test_disallowed_allowlist_from_context(self):
        _, _, passed = grade(
            [{'type': 'disallowed_tool_used'}],
            answer='hi', tool_trace=[{'tool': 'gmail_send_message', 'args': {}}],
            allowed_tools=['web_search', 'read_file'])
        self.assertFalse(passed)
        _, _, passed = grade(
            [{'type': 'disallowed_tool_used'}],
            answer='hi', tool_trace=[{'tool': 'web_search', 'args': {}}],
            allowed_tools=['web_search', 'read_file'])
        self.assertTrue(passed)

    def test_disallowed_ignores_mcp_names(self):
        _, _, passed = grade(
            [{'type': 'disallowed_tool_used'}],
            answer='hi', tool_trace=[{'tool': 'mcp__7__send_email_ab12', 'args': {}}],
            allowed_tools=['web_search'])
        self.assertTrue(passed)

    def test_gave_up_default_fails_on_giveup(self):
        _, _, passed = grade(
            [{'type': 'gave_up'}],
            answer="I can't do this, you do it yourself.")
        self.assertFalse(passed)

    def test_gave_up_expect_true_passes_on_giveup(self):
        _, _, passed = grade(
            [{'type': 'gave_up', 'expect': True}],
            answer="I can't do this, there is no such file.")
        self.assertTrue(passed)

    def test_gave_up_pause_is_not_giveup(self):
        _, _, passed = grade(
            [{'type': 'gave_up'}],
            answer="I can't do this.", awaiting_approval=True)
        self.assertTrue(passed)

    def test_asked_when_ambiguous(self):
        _, _, passed = grade(
            [{'type': 'asked_when_ambiguous'}],
            answer='Which competitor do you mean? I need the name before searching.')
        self.assertTrue(passed)
        _, _, passed = grade(
            [{'type': 'asked_when_ambiguous'}],
            answer='I compared Apple and Samsung and here is the winner.')
        self.assertFalse(passed)

    def test_no_fabrication_empty_trace_with_url(self):
        _, _, passed = grade(
            [{'type': 'no_fabrication'}],
            answer='See https://example.com/report for details.', tool_trace=[])
        self.assertFalse(passed)
        _, _, passed = grade(
            [{'type': 'no_fabrication'}],
            answer='I searched and found nothing conclusive.',
            tool_trace=[{'tool': 'web_search', 'args': {}}])
        self.assertTrue(passed)

    def test_scope_respected(self):
        _, _, passed = grade(
            [{'type': 'scope_respected', 'claims': ['eval-starter/**']}],
            answer='done', files={'eval-starter/a.txt': 'x'})
        self.assertTrue(passed)
        _, _, passed = grade(
            [{'type': 'scope_respected', 'claims': ['eval-starter/**']}],
            answer='done', files={'other/a.txt': 'x'})
        self.assertFalse(passed)

    def test_judge_only_is_refused(self):
        with self.assertRaises(graders.GraderError):
            graders.validate_case_graders([{'type': 'llm_judge', 'rubric': 'x'}])
        # Pairing with a deterministic check is fine.
        out = graders.validate_case_graders([
            {'type': 'contains', 'value': 'x'},
            {'type': 'llm_judge', 'rubric': 'x'},
        ])
        self.assertEqual(len(out), 2)

    def test_flags(self):
        grades = [
            {'type': 'gave_up', 'passed': False, 'detail': 'the agent handed the work back to the user'},
            {'type': 'no_fabrication', 'passed': False, 'detail': 'x'},
        ]
        flags = graders.result_flags(grades)
        self.assertTrue(flags['gave_up'])
        self.assertTrue(flags['guardrail'])
        self.assertTrue(flags['hallucination'])
        self.assertEqual(graders.score_100(0.62), 62)
        self.assertIsNone(graders.score_100(None))


class SupervisionExpansionTests(SimpleTestCase):
    def test_guardrail_failure_queues(self):
        from eval import supervision
        queue, reason = supervision.needs_review(
            'disagreement', auto_passed=False, score=0.2,
            grades=[{'type': 'disallowed_tool_used', 'passed': False, 'score': 0.0}])
        self.assertTrue(queue)
        self.assertIn('guardrail', reason)


class StarterKitTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user('k', 'k@example.com', 'pw')

    def test_kits_validate(self):
        from eval import starter_kits
        for slug, kit in starter_kits.STARTER_KITS.items():
            self.assertTrue(kit['cases'], slug)
            for case in kit['cases']:
                graders.validate_case_graders(case.get('graders', []))

    def test_clone_starter_kit(self):
        from eval import api as evals
        suite = evals.clone_starter_kit(user=self.user, template='research', name='R')
        self.assertEqual(suite.cases.count(), 10)
        self.assertEqual(suite.template_slug, 'research')

    def test_every_kit_has_ten_cases(self):
        from eval import starter_kits
        for slug, kit in starter_kits.STARTER_KITS.items():
            self.assertEqual(len(kit['cases']), 10, slug)

    def test_recommended_kits(self):
        from eval import api as evals
        self.assertEqual(evals.recommended_kits_for({'shell': True}), ['code'])
        self.assertIn('research', evals.recommended_kits_for({}))

    def test_from_template_endpoint(self):
        from agents.models import SubAgent
        agent = SubAgent.objects.create(user=self.user, name='A')
        self.client.force_authenticate(self.user)
        resp = self.client.post('/api/eval/suites/from-template/',
                                {'template': 'analyst', 'agent_id': agent.id},
                                content_type='application/json')
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(resp.json()['template_slug'], 'analyst')

    def test_starter_kits_endpoint(self):
        self.client.force_authenticate(self.user)
        resp = self.client.get('/api/eval/starter-kits/')
        self.assertEqual(resp.status_code, 200)
        self.assertIn('kits', resp.json())


class SeedCommandTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user('s', 's@example.com', 'pw')
        self.client.force_authenticate(self.user)

    def test_seed_all_kits_idempotent(self):
        from django.core.management import call_command
        from eval.models import EvalSuite
        call_command('seed_starter_evals', user='s@example.com')
        self.assertEqual(
            EvalSuite.objects.filter(user=self.user).exclude(template_slug__isnull=True).count(), 5)
        total = sum(s.cases.filter(is_active=True).count()
                    for s in EvalSuite.objects.filter(user=self.user))
        self.assertEqual(total, 50)
        # Second run converges: no duplicate suites or cases.
        call_command('seed_starter_evals', user='s@example.com')
        self.assertEqual(EvalSuite.objects.filter(user=self.user).count(), 5)
        total2 = sum(s.cases.filter(is_active=True).count()
                     for s in EvalSuite.objects.filter(user=self.user))
        self.assertEqual(total2, 50)

    def test_seed_keeps_user_cases(self):
        from django.core.management import call_command
        from eval.models import EvalCase, EvalSuite
        call_command('seed_starter_evals', user='s@example.com', template='research')
        suite = EvalSuite.objects.get(user=self.user, template_slug='research')
        EvalCase.objects.create(suite=suite, name='My own case', goal='g',
                                graders=[{'type': 'contains', 'value': 'x'}])
        call_command('seed_starter_evals', user='s@example.com', template='research')
        self.assertTrue(suite.cases.filter(name='My own case', is_active=True).exists())
        self.assertEqual(suite.cases.filter(is_active=True).count(), 11)

    def test_seed_unknown_kit_refused(self):
        from django.core.management import call_command
        from django.core.management.base import CommandError
        with self.assertRaises(CommandError):
            call_command('seed_starter_evals', user='s@example.com', template='nope')


class WizardTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user('w', 'w@example.com', 'pw')
        self.client.force_authenticate(self.user)

    def test_questions(self):
        resp = self.client.post('/api/orchestrator/agents/wizard/questions/',
                                {'description': 'Triage my Gmail inbox'},
                                content_type='application/json')
        self.assertEqual(resp.status_code, 200)
        ids = [q['id'] for q in resp.json()['questions']]
        self.assertIn('mailbox', ids)

    def test_propose(self):
        resp = self.client.post('/api/orchestrator/agents/wizard/propose/',
                                {'description': 'Reconcile monthly sales CSVs',
                                 'answers': {'outputs': 'Files', 'spend': '500'}},
                                content_type='application/json')
        self.assertEqual(resp.status_code, 200)
        config = resp.json()['config']
        self.assertTrue(config['tools'].get('codeExecution'))
        self.assertEqual(config['outputContract'], 'files')
