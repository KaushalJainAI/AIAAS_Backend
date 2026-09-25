"""
E-5: the chat front door to evals.

Chat starts things ("test my invoice agent") but only ever makes drafts:
worlds, cases and run imports arrive `needs-review`, and accepting happens
on the Evals page only — there is deliberately no accept tool. All five
model-derived write paths go through `eval/api.py::save_cases`, so the
draft rule lives in one place. No provider anywhere here.
"""
from __future__ import annotations

import json
from unittest.mock import patch

from asgiref.sync import async_to_sync
from django.contrib.auth.models import User
from django.test import TestCase

from agents.models import SubAgent
from eval import api as evals
from eval.models import EvalCase, EvalSuite, EvalWorld
from logs.models import ExecutionLog


def _user(name='chatowner'):
    return User.objects.create_user(name, f'{name}@example.com', 'pw')


def _agent(user, name='Analyst'):
    return SubAgent.objects.create(
        user=user, name=name,
        tool_grants={'fileOps': True}, sandbox={'fileAccess': 'scoped'},
        guardrails={'autonomy': 'ask'}, prompt='You close books.')


async def _finish(coro):
    return await coro


def _suite(user, agent, name='Close'):
    return EvalSuite.objects.create(user=user, name=name, subagent=agent)


class SaveCasesTests(TestCase):
    def setUp(self):
        self.user = _user()
        self.agent = _agent(self.user)
        self.suite = _suite(self.user, self.agent)

    def test_drafts_are_inactive_tagged_and_versioned(self):
        rows = evals.save_cases(self.suite, [{
            'name': 'Q?', 'goal': 'Report it.',
            'reference': 'Good.', 'graders': [{'type': 'no_error'}],
            'tags': ['generated', 'normal'],
        }], drafts=True)
        self.assertEqual(len(rows), 1)
        case = EvalCase.objects.get(pk=rows[0].pk)
        self.assertFalse(case.is_active)
        self.assertIn('needs-review', case.tags)
        self.assertIn('generated', case.tags)

    def test_active_path_stays_for_explicit_use(self):
        rows = evals.save_cases(self.suite, [{
            'goal': 'Report it.', 'graders': [{'type': 'no_error'}]}],
            drafts=False)
        self.assertTrue(EvalCase.objects.get(pk=rows[0].pk).is_active)

    def test_unknown_graders_are_a_grader_error(self):
        with self.assertRaises(evals.GraderError):
            evals.save_cases(self.suite, [{
                'goal': 'g', 'graders': [{'type': 'nope'}]}], drafts=True)

    def test_judge_never_alone_enforced(self):
        with self.assertRaises(evals.GraderError):
            evals.save_cases(self.suite, [{
                'goal': 'g',
                'graders': [{'type': 'llm_judge', 'rubric': 'good?'}]}],
                drafts=True)

    def test_orders_continue_and_only_world_generations_stamp(self):
        EvalCase.objects.create(suite=self.suite, goal='old', order=7)
        EvalWorld.objects.create(
            suite=self.suite, version=1, status='accepted', brief='b')
        # An import or an added case is about the agent's real situation: no
        # version, so it runs outside the world and never goes stale.
        rows = evals.save_cases(self.suite, [{'goal': 'new'}], drafts=True)
        self.assertEqual(rows[0].order, 8)
        self.assertIsNone(rows[0].world_version)
        built = evals.save_cases(self.suite, [{'goal': 'w'}], drafts=True,
                                 world_version=1)
        self.assertEqual(built[0].world_version, 1)


class AddCaseToolTests(TestCase):
    def setUp(self):
        self.user = _user('adder')
        self.agent = _agent(self.user)
        self.suite = _suite(self.user, self.agent)

    def _call(self, **args):
        from chat.tools.eval_manager import add_eval_case
        return json.loads(async_to_sync(add_eval_case)(
            args, {'user_id': self.user.id}))

    def test_saves_a_draft_with_a_review_pointer(self):
        out = self._call(suite_id=self.suite.id, goal='Report Q3.',
                         graders=[{'type': 'contains', 'value': 'Q3'}])
        self.assertTrue(out['draft'])
        self.assertIn('Evals', out['review'])
        case = EvalCase.objects.get(pk=out['case_id'])
        self.assertFalse(case.is_active)

    def test_bad_graders_are_an_error_not_a_row(self):
        out = self._call(suite_id=self.suite.id, goal='g',
                         graders=[{'type': 'llm_judge', 'rubric': 'x'}])
        self.assertIn('error', out)
        self.assertEqual(self.suite.cases.count(), 0)

    def test_foreign_suite_refused(self):
        other = User.objects.create_user('o', 'o@example.com', 'pw')
        suite = EvalSuite.objects.create(user=other, name='Theirs')
        out = self._call(suite_id=suite.id, goal='g')
        self.assertIn('error', out)


class CreateSuiteToolTests(TestCase):
    def setUp(self):
        self.user = _user('creator')
        self.agent = _agent(self.user)

    def test_creates_empty_suite_for_the_agent(self):
        from chat.tools.eval_manager import create_eval_suite
        out = json.loads(async_to_sync(create_eval_suite)(
            {'agent_id': self.agent.id}, {'user_id': self.user.id}))
        suite = EvalSuite.objects.get(pk=out['suite_id'])
        self.assertEqual(suite.subagent_id, self.agent.id)
        self.assertEqual(suite.cases.count(), 0)
        # Name collisions suffix rather than 500.
        again = json.loads(async_to_sync(create_eval_suite)(
            {'agent_id': self.agent.id, 'name': suite.name},
            {'user_id': self.user.id}))
        self.assertNotEqual(again['suite_id'], out['suite_id'])


class GenerateWorldToolTests(TestCase):
    def setUp(self):
        self.user = _user('genchat')
        self.agent = _agent(self.user)
        self.suite = _suite(self.user, self.agent)

    def _call(self, **args):
        from chat.tools.eval_manager import generate_eval_world
        return json.loads(async_to_sync(generate_eval_world)(
            args, {'user_id': self.user.id}))

    def _start(self, generate, **args):
        """Call the tool, then run the background task it spawned to the end."""
        spawned = []
        with patch('workflow_backend.background.spawn',
                   side_effect=lambda coro, **k: spawned.append(coro)),                 patch('eval.generator.generate_world', side_effect=generate):
            out = self._call(**args)
            for coro in spawned:
                async_to_sync(_finish)(coro)
        return out

    def test_starts_in_background_then_saves_drafts(self):
        out_payload = {
            'brief': 'Acme close.', 'facts': [], 'surfaces': {'files': True},
            'fixtures': {'files': {'n.md': 'x'}}, 'model': 'judge',
            'cases': [dict(name='Q?', category='normal', goal='Report.',
                           input_data={}, reference='Good.',
                           graders=[{'type': 'no_error'}])],
            'rejected': [], 'tokens': 10, 'cost_usd': None,
        }

        async def fake(*a, **k):
            return out_payload

        out = self._start(fake, suite_id=self.suite.id, focus='close', cases=3)
        self.assertEqual(out['version'], 1)
        self.assertEqual(out['status'], 'generating')
        self.assertIn('Evals', out['review'])
        world = EvalWorld.objects.get(pk=out['world_id'])
        self.assertEqual(world.status, 'draft')
        self.assertEqual((world.focus, world.requested_cases), ('close', 3))
        case = EvalCase.objects.get(suite=self.suite)
        self.assertFalse(case.is_active)
        self.assertEqual(case.world_version, 1)

    def test_judge_failure_marks_the_world_failed(self):
        async def broken(*a, **k):
            raise ValueError('no JSON')

        out = self._start(broken, suite_id=self.suite.id)
        world = EvalWorld.objects.get(pk=out['world_id'])
        self.assertEqual(world.status, 'failed')
        self.assertIn('no JSON', world.error_message)
        self.assertFalse(EvalCase.objects.exists())

    def test_second_generation_while_one_runs_is_refused(self):
        EvalWorld.objects.create(suite=self.suite, version=1, status='generating')
        out = self._call(suite_id=self.suite.id)
        self.assertIn('already being generated', out['error'])

    def test_agent_with_nothing_to_simulate_is_refused_up_front(self):
        self.agent.tool_grants = {}
        self.agent.sandbox = {'fileAccess': 'none'}
        self.agent.save()
        out = self._call(suite_id=self.suite.id)
        self.assertIn('nothing a generated world can hold', out['error'])
        self.assertFalse(EvalWorld.objects.exists())


class ImportRunsToolTests(TestCase):
    def setUp(self):
        self.user = _user('importer')
        self.agent = _agent(self.user)
        self.suite = _suite(self.user, self.agent)
        ExecutionLog.objects.create(
            subagent=self.agent, user=self.user, status='completed',
            input_data={'goal': 'do it'}, output_data={'answer': 'done'})

    def test_imports_drafts(self):
        from chat.tools.eval_manager import import_eval_cases_from_runs
        out = json.loads(async_to_sync(import_eval_cases_from_runs)(
            {'suite_id': self.suite.id}, {'user_id': self.user.id}))
        self.assertEqual(out['cases'], 1)
        self.assertTrue(out['draft'])
        case = EvalCase.objects.get(suite=self.suite)
        self.assertFalse(case.is_active)
        self.assertIn('needs-review', case.tags)
        # Second import skips the already-imported run.
        again = json.loads(async_to_sync(import_eval_cases_from_runs)(
            {'suite_id': self.suite.id}, {'user_id': self.user.id}))
        self.assertEqual(again['cases'], 0)
        self.assertEqual(again['already_imported'], 1)


class ResultsToolTests(TestCase):
    def setUp(self):
        from eval import runner
        self.user = _user('reader')
        self.agent = _agent(self.user)
        self.suite = _suite(self.user, self.agent)
        self.case = EvalCase.objects.create(
            suite=self.suite, goal='g',
            graders=[{'type': 'contains', 'value': 'x'}])
        self.run = async_to_sync(runner.open_run)(
            self.suite, self.agent, self.user, '')

        async def fake(agent, goal, **kwargs):
            from agents.agent.runtime import AgentRun
            return AgentRun(
                execution_id='00000000-0000-0000-0000-000000000004',
                answer='nothing here', thinking='', tool_trace=[],
                tokens=5, awaiting_approval=False, unserved_grants=(),
                duration_ms=5)

        with patch('agents.agent.runtime.run_agent', fake):
            async_to_sync(runner.sweep)(
                self.run, self.suite, self.agent, self.user)
        self.run.refresh_from_db()

    def test_explains_each_case(self):
        from chat.tools.eval_manager import get_eval_results
        out = json.loads(async_to_sync(get_eval_results)(
            {'run_id': str(self.run.run_id)}, {'user_id': self.user.id}))
        self.assertEqual(len(out['cases']), 1)
        row = out['cases'][0]
        self.assertFalse(row['passed'])
        self.assertTrue(any('contains' in f for f in row['failing']))
        self.assertIn('nothing here', row['answer'])

    def test_unknown_run_is_an_error(self):
        from chat.tools.eval_manager import get_eval_results
        out = json.loads(async_to_sync(get_eval_results)(
            {'run_id': '00000000-0000-0000-0000-000000000000'},
            {'user_id': self.user.id}))
        self.assertIn('error', out)


class EvalCommandTests(TestCase):
    def setUp(self):
        from chat.models import ChatSession
        self.user = _user('chatter')
        self.session = ChatSession.objects.create(user=self.user, title='t')
        self._msg('user', 'What was Q3 revenue?')
        self._msg('assistant', 'Q3 revenue was 412300.')

    def _msg(self, role, content):
        from chat.models import ChatMessage
        return ChatMessage.objects.create(
            session=self.session, role=role, content=content)

    def test_saves_question_as_goal_and_answer_as_reference_draft(self):
        from chat.commands.library import eval_command
        from chat.commands.registry import CommandCall, CommandContext
        result = async_to_sync(eval_command)(
            CommandCall(name='eval', args={}, text=''),
            CommandContext(user_id=self.user.id,
                           session_id=str(self.session.id)))
        self.assertEqual(result.status, 'ok')
        self.assertTrue(result.card['draft'])
        case = EvalCase.objects.get(pk=result.card['case_id'])
        self.assertEqual(case.goal, 'What was Q3 revenue?')
        self.assertIn('412300', case.reference)
        self.assertFalse(case.is_active)
        self.assertIn('needs-review', case.tags)

    def test_no_answer_is_an_error(self):
        from chat.commands.library import eval_command
        from chat.commands.registry import CommandCall, CommandContext
        from chat.models import ChatSession
        empty = ChatSession.objects.create(user=self.user, title='e')
        result = async_to_sync(eval_command)(
            CommandCall(name='eval', args={}, text=''),
            CommandContext(user_id=self.user.id,
                           session_id=str(empty.id)))
        self.assertEqual(result.status, 'error')


class DescribeCostNoteTests(TestCase):
    def test_generate_world_card_names_the_spend(self):
        from chat.tools.describe import describe_call
        card = describe_call('generate_eval_world',
                             {'suite_id': 1, 'cases': 12})
        self.assertIn('judge-model calls', card['sentence'])
        self.assertIn('drafts', card['sentence'])
