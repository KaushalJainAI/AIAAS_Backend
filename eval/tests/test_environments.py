"""
E-1: eval worlds — hidden fixtures, confined runs, withheld tools, generation.

A world is a judge-built fake situation a suite's cases share. These tests pin
the E-1 foundation without any provider: the hidden `/.eval/` tree, per-attempt
prepare/snapshot, scope confinement, fail-closed withholding, the generation
pipeline (judge steps mocked), and the review rules (world before cases, stale
cases never swept).
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from asgiref.sync import async_to_sync, sync_to_async
from django.contrib.auth.models import User
from django.core.files.base import ContentFile
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from rest_framework.test import APITestCase

from agents.agent.runtime import AgentRun
from agents.models import SubAgent
from eval import environment as envmod
from eval import graders
from eval import runner
from eval.environment import EvalEnvironment
from eval.generator import (
    clean_env_case,
    generate_world,
    prove_case,
    surfaces_for_agent,
)
from eval.models import EvalCase, EvalSuite, EvalWorld
from inference import filesystem as fs
from inference import vfs


def _user(name='owner'):
    return User.objects.create_user(name, f'{name}@example.com', 'pw')


def _agent(user, name='Analyst', **kwargs):
    defaults = dict(
        tool_grants={'fileOps': True},
        sandbox={'fileAccess': 'read_all_write_own'},
        guardrails={'autonomy': 'ask'},
        prompt='You close the books.',
    )
    defaults.update(kwargs)
    return SubAgent.objects.create(user=user, name=name, **defaults)


def _suite(user, agent, name='Close'):
    return EvalSuite.objects.create(
        user=user, name=name, subagent=agent, supervision='failures')


def _world(suite, version=1, status='accepted', **kwargs):
    defaults = dict(
        brief='Acme Tools, Q3 close in progress.',
        surfaces={'files': True},
        fixtures={'files': {
            'orders.csv': 'id,amount\n1,412300\n2,10100\n',
            'notes.md': 'Q3 revenue is 412300. Invoice INV-1043 appears twice.\n',
        }},
        facts=[
            {'key': 'q3_revenue', 'value': '412300',
             'statement': 'Q3 revenue is 412300.'},
            {'key': 'duplicate_invoice', 'value': 'INV-1043',
             'statement': 'Invoice INV-1043 appears twice.'},
        ],
    )
    defaults.update(kwargs)
    return EvalWorld.objects.create(suite=suite, version=version, status=status,
                                    **defaults)


def _agent_run(answer='done', **kwargs):
    defaults = dict(
        execution_id='00000000-0000-0000-0000-000000000000',
        answer=answer, thinking='', tool_trace=[], tokens=10,
        awaiting_approval=False, unserved_grants=(), duration_ms=50,
    )
    return AgentRun(**{**defaults, **kwargs})


# ---------------------------------------------------------------- hiding


class HiddenTreeTests(TestCase):
    """`/.eval/` never lists, but stays readable by id and by the eval code."""

    def setUp(self):
        self.user = _user()
        root = fs.ensure_folder(self.user, fs.EVAL_ROOT_NAME, None)
        self.inner = fs.ensure_folder(self.user, 's1', root)
        from inference.models import Document
        Document.objects.create(
            user=self.user, folder=self.inner, name='orders.csv',
            file='', file_type='csv', file_size=3, content_text='id\n1\n',
            status='stored')

    def test_children_hides_the_eval_root(self):
        names = [f.name for f in fs.children(self.user, None)]
        self.assertNotIn(fs.EVAL_ROOT_NAME, names)

    def test_child_by_name_still_reaches_it(self):
        # The eval code walks by name; hiding is listing-only.
        self.assertIsNotNone(fs.child_by_name(self.user, None, fs.EVAL_ROOT_NAME))

    def test_owned_documents_exclude_fixtures(self):
        from inference.views import _owned_documents
        names = [d.name for d in _owned_documents(self.user)]
        self.assertNotIn('orders.csv', names)

    def test_find_excludes_fixtures_for_tree_scopes(self):
        scope = vfs.build_scope(self.user, 'full')
        out = vfs.find(scope, 'orders.csv')
        self.assertEqual(out['matches'], [])

    def test_find_searches_inside_a_world_scope(self):
        world_folder = fs.child_by_name(self.user, None, fs.EVAL_ROOT_NAME)
        scope = vfs.FileScope(user=self.user, root=self.inner, mode='scoped',
                              label='/', write_prefix=(), write_label='/')
        out = vfs.find(scope, 'orders')
        self.assertEqual(len(out['matches']), 1)
        self.assertEqual(world_folder.name, fs.EVAL_ROOT_NAME)


# ---------------------------------------------------------------- prepare


class PrepareSnapshotTests(TestCase):
    def setUp(self):
        self.user = _user()
        self.agent = _agent(self.user)
        self.suite = _suite(self.user, self.agent)
        self.world = _world(self.suite)
        # An owner file that must stay unreachable inside the world.
        from inference.models import Document
        Document.objects.create(
            user=self.user, folder=None, name='owner_secret.md',
            file='', file_type='md', file_size=5, content_text='salary 999999',
            status='stored')

    def _env(self):
        env = envmod.for_attempt(self.user, self.agent, self.suite, None)
        self.assertIsNotNone(env)
        return env

    def test_no_world_means_no_environment(self):
        self.world.delete()
        self.assertIsNone(envmod.for_attempt(self.user, self.agent, self.suite, None))

    def test_prepare_writes_fixtures_and_overrides(self):
        env = self._env()
        spec = {'root': 'work/x', 'files': {'extra.md': 'case file'}}
        path = env.prepare(None, spec, 'r1')
        self.assertEqual(path, '/')
        scope = env.attempt_scope()
        self.assertTrue(
            vfs.read_file(scope, '/orders.csv')['content'].startswith('id,amount\n1,412300'))
        self.assertIn('case file', vfs.read_file(scope, '/extra.md')['content'])

    def test_deleting_a_result_removes_only_its_hidden_attempt_folder(self):
        env = self._env()
        env.prepare(None, None, '101')
        env.prepare(None, None, '102')
        root = fs.eval_root(self.user)
        suite_folder = fs.child_by_name(self.user, root, f's{self.suite.id}')
        version_folder = fs.child_by_name(
            self.user, suite_folder, f'v{self.world.version}',
        )
        attempts = fs.child_by_name(self.user, version_folder, 'attempts')

        removed = envmod.delete_attempts(
            self.user, self.suite.id, self.world.version, [101],
        )

        self.assertEqual(removed, 1)
        self.assertIsNone(fs.child_by_name(self.user, attempts, '101'))
        self.assertIsNotNone(fs.child_by_name(self.user, attempts, '102'))

    def test_deleting_attempt_removes_binary_file_bytes_from_storage(self):
        from inference.models import Document

        env = self._env()
        with tempfile.TemporaryDirectory() as media_root:
            with override_settings(MEDIA_ROOT=media_root):
                env.prepare(None, None, '103')
                document = Document.objects.create(
                    user=self.user, folder=env.attempt_folder, name='report.xlsx',
                    file='', file_type='xlsx', file_size=0, content_text='',
                    status='stored',
                )
                document.file.save('report.xlsx', ContentFile(b'PK\x03\x04binary'), save=True)
                file_path = Path(document.file.path)
                self.assertTrue(file_path.exists())

                envmod.delete_attempts(
                    self.user, self.suite.id, self.world.version, [103],
                )

                self.assertFalse(file_path.exists())

    def test_attempt_scope_is_confined(self):
        env = self._env()
        env.prepare(None, None, 'r1')
        scope = env.attempt_scope()
        listed = vfs.list_dir(scope, '/')
        names = {f['name'] for f in listed['files']}
        self.assertEqual(names, {'orders.csv', 'notes.md'})
        # The owner's file is not addressable: `..` clamps at the root.
        with self.assertRaises(vfs.VfsError):
            vfs.read_file(scope, '/owner_secret.md')
        with self.assertRaises(vfs.VfsError):
            vfs.read_file(scope, '/../owner_secret.md')
        self.assertEqual(vfs.find(scope, 'salary')['matches'], [])
        # ... but the world itself is searchable and writable.
        self.assertEqual(len(vfs.find(scope, '412300')['matches']), 2)
        vfs.write_file(scope, '/summary.md', 'Q3 was 412300.')
        files, _ = env.snapshot_files()
        self.assertIn('summary.md', files)

    def test_snapshot_and_changes(self):
        env = self._env()
        env.prepare(None, None, 'r1')
        scope = env.attempt_scope()
        vfs.write_file(scope, '/notes.md', 'Q3 revenue is 412300. Closed.\n')
        vfs.write_file(scope, '/new.md', 'new')
        files, binaries = env.snapshot_files()
        self.assertEqual(binaries, {})
        self.assertIn('Closed.', files['notes.md'])
        delta = env.changes(files)
        self.assertEqual(sorted(delta['files_written']), ['new.md', 'notes.md'])
        self.assertNotIn('files_removed', delta)


# ---------------------------------------------------------------- withholding


class WithheldTests(TestCase):
    def setUp(self):
        self.user = _user()
        self.agent = _agent(
            self.user, tool_grants={'fileOps': True, 'codeExecution': True,
                                    'webSearch': True, 'rag': True, 'mcp': True},
            agent_context={'knowledgeBases': []})
        self.suite = _suite(self.user, self.agent)
        self.world = _world(self.suite)

    def _toolbox(self):
        from agents.agent.runtime import AgentToolbox
        env = envmod.for_attempt(self.user, self.agent, self.suite, None)
        env.prepare(None, None, 'r1')
        return AgentToolbox.for_agent(
            self.agent, self.user.id, file_scope=env.attempt_scope(),
            session_key='t1', environment=env), env

    def test_risky_tools_withheld_safe_ones_kept(self):
        box, env = self._toolbox()
        allowed = set(box.allowed_names)
        for name in ('web_search', 'knowledge_base_search', 'keyword_search',
                     'list_documents', 'read_document'):
            self.assertNotIn(name, allowed, name)
        for name in ('list_files', 'read_file', 'write_file', 'find_files',
                     'update_todos', 'ask_user', 'execute_python'):
            self.assertIn(name, allowed, name)
        self.assertFalse(box.mcp_allowed)

    def test_descriptors_never_offer_withheld_tools(self):
        box, env = self._toolbox()
        names = {d['function']['name']
                 for d in async_to_sync(box.descriptors)()}
        self.assertNotIn('web_search', names)
        self.assertNotIn('knowledge_base_search', names)
        self.assertIn('read_file', names)

    def test_dispatch_refuses_what_descriptors_withhold(self):
        box, env = self._toolbox()
        reply = async_to_sync(box.dispatch)(
            'web_search', {'query': 'x'}, {'user_id': self.user.id})
        self.assertIn('not granted', reply.lower().replace("'", ''))

    def test_simulated_names_survive_withholding(self):
        env = EvalEnvironment(
            user=self.user, agent=self.agent, suite=self.suite,
            world=SimpleNamespace(fixtures={}, surfaces={}, version=1))
        env.sims = {'mail': SimpleNamespace(
            TOOLS=('gmail_search_threads',),
            handles=lambda n: n == 'gmail_search_threads',
            run=lambda n, a: '{"type": "gmail_threads", "threads": []}',
            snapshot=lambda: {}, changes=lambda: {})}
        self.assertTrue(env.simulates('gmail_search_threads'))
        self.assertFalse(env.simulates('gmail_send_message'))
        kept = {'gmail_search_threads', 'gmail_send_message', 'read_file'}
        self.assertEqual(env.withheld_names(set(kept)), {'gmail_send_message'})
        reply = async_to_sync(env.run_simulated)(
            'gmail_search_threads', {'query': 'x'}, {})
        self.assertIn('gmail_threads', reply)
        self.assertEqual(env.calls[0]['tool'], 'gmail_search_threads')


# ---------------------------------------------------------------- sweep


class WorldSweepTests(TestCase):
    def setUp(self):
        self.user = _user()
        self.agent = _agent(self.user)
        self.suite = _suite(self.user, self.agent)
        self.world = _world(self.suite)

    def _case(self, goal='Report Q3 revenue from {workspace}/notes.md', **kwargs):
        defaults = dict(
            suite=self.suite, goal=goal, is_active=True, world_version=1,
            graders=[{'type': 'file_contains', 'path': 'summary.md',
                      'value': '412300'}],
        )
        return EvalCase.objects.create(**{**defaults, **kwargs})

    def test_sweep_runs_inside_the_world(self):
        self._case()
        seen = {}

        async def fake(agent, goal, **kwargs):
            from inference import vfs as _vfs
            env = kwargs.get('environment')
            seen['env'] = env is not None
            self.assertIn('Report', goal)
            await sync_to_async(_vfs.write_file)(
                env.attempt_scope(), '/summary.md', 'Q3: 412300\n')
            return _agent_run('wrote it')

        run = async_to_sync(runner.open_run)(self.suite, self.agent, self.user, '')
        with patch('agents.agent.runtime.run_agent', fake):
            async_to_sync(runner.sweep)(run, self.suite, self.agent, self.user)
        run.refresh_from_db()
        self.assertTrue(seen['env'])
        self.assertEqual(run.world_version, 1)
        self.assertEqual(run.status, 'completed')
        result = run.results.get()
        self.assertTrue(result.auto_passed)
        self.assertEqual(result.env_changes['files_written'], ['summary.md'])

    def test_stale_cases_are_never_swept(self):
        self._case()
        self._case(goal='old world task', world_version=99)
        run = async_to_sync(runner.open_run)(self.suite, self.agent, self.user, '')

        async def fake(agent, goal, **kwargs):
            return _agent_run('x')

        with patch('agents.agent.runtime.run_agent', fake):
            async_to_sync(runner.sweep)(run, self.suite, self.agent, self.user)
        run.refresh_from_db()
        self.assertEqual(run.results.count(), 1)
        self.assertEqual(run.results.get().goal, self.suite.cases.get(world_version=1).goal)

    def test_legacy_cases_without_a_version_still_run(self):
        self._case(world_version=None)
        run = async_to_sync(runner.open_run)(self.suite, self.agent, self.user, '')

        async def fake(agent, goal, **kwargs):
            self.assertIsNotNone(kwargs.get('environment'))
            return _agent_run('x')

        with patch('agents.agent.runtime.run_agent', fake):
            async_to_sync(runner.sweep)(run, self.suite, self.agent, self.user)
        self.assertEqual(run.results.count(), 1)


# ---------------------------------------------------------------- validation


class WorldValidationTests(SimpleTestCase):
    def _parts(self, **kwargs):
        base = dict(
            surfaces={'files': True},
            fixtures={'files': {'a.md': 'Q3 revenue is 412300'}},
            facts=[{'key': 'q3', 'value': '412300', 'statement': 'Q3 is 412300'}],
        )
        base.update(kwargs)
        return base

    def test_valid_world_has_no_errors(self):
        parts = self._parts()
        self.assertEqual(
            envmod.validate_world(parts['surfaces'], parts['fixtures'], parts['facts']), [])

    def test_caps_and_paths_and_types(self):
        parts = self._parts(fixtures={'files': {f'f{i}.md': 'x' for i in range(31)}})
        errors = envmod.validate_world(parts['surfaces'], parts['fixtures'], parts['facts'])
        self.assertTrue(any('too many files' in e for e in errors))
        bad = self._parts(fixtures={'files': {'../evil.md': 'x', 'b.xlsx': 'x'}})
        errors = envmod.validate_world(bad['surfaces'], bad['fixtures'], bad['facts'])
        self.assertTrue(any('unsafe' in e for e in errors))
        self.assertTrue(any('binary' in e for e in errors))
        self.assertTrue(envmod.validate_world(
            {'files': True}, {'files': {}}, []))

    def test_fact_coverage_normalises_numbers(self):
        self.assertTrue(envmod.fact_covered('412,300', 'revenue 412300 ok'))
        self.assertFalse(envmod.fact_covered('INV-1043', 'nothing here'))

    def test_surfaces_for_agent(self):
        agent = SimpleNamespace(
            tool_grants={'fileOps': True, 'rag': True, 'mcp': True,
                         'webSearch': True},
            sandbox={'fileAccess': 'scoped'})
        surfaces = surfaces_for_agent(agent)
        self.assertTrue(surfaces['files'])
        self.assertTrue(surfaces['kb'])
        self.assertTrue(surfaces['mail'])
        self.assertTrue(surfaces['calendar'])
        self.assertTrue(surfaces['drive'])
        self.assertTrue(surfaces['web'])
        bare = SimpleNamespace(tool_grants={}, sandbox={})
        self.assertEqual(surfaces_for_agent(bare), {})


# ---------------------------------------------------------------- cases


class CleanEnvCaseTests(SimpleTestCase):
    FACTS = [
        {'key': 'q3_revenue', 'value': '412300', 'statement': 'Q3 is 412300.'},
    ]
    TOOLS = {'read_file', 'write_file', 'list_files', 'find_files'}

    def _raw(self, **kwargs):
        base = dict(
            name='Revenue?', category='normal', goal='Report Q3 revenue.',
            input_data={}, facts_used=['q3_revenue'], expected='412300',
            expect_state={'files': {'summary.md': 'Q3: 412300\n'}},
            graders=[{'type': 'file_contains', 'path': 'summary.md',
                      'value': '412300'}],
        )
        base.update(kwargs)
        return base

    def test_valid_case_carries_facts_and_reference(self):
        case, why = clean_env_case(self._raw(), self.TOOLS, self.FACTS)
        self.assertEqual(why, '')
        self.assertIn('412300', case['reference'])
        self.assertIn('Q3 is 412300.', case['reference'])
        self.assertEqual(case['input_data']['__facts__'], ['q3_revenue'])
        self.assertEqual(
            case['input_data']['__expect_state__']['files']['summary.md'],
            'Q3: 412300\n')

    def test_unknown_facts_rejected(self):
        case, why = clean_env_case(
            self._raw(facts_used=['nope']), self.TOOLS, self.FACTS)
        self.assertIsNone(case)
        self.assertIn('unknown facts', why)

    def test_missing_expected_rejected(self):
        case, why = clean_env_case(
            self._raw(expected=''), self.TOOLS, self.FACTS)
        self.assertIsNone(case)
        self.assertIn('no expected answer', why)

    def test_state_claim_without_a_grader_rejected(self):
        raw = self._raw(expect_state={'files': {'s.md': 'x'},
                                      'sent': [{'to': 'a@b.c'}]})
        case, why = clean_env_case(raw, self.TOOLS, self.FACTS)
        self.assertIsNone(case)
        self.assertIn('expects sent changes nothing checks', why)

    def test_anchors_alone_prove_nothing(self):
        raw = self._raw(graders=[{'type': 'no_error'}],
                        expect_state={})
        case, why = clean_env_case(raw, self.TOOLS, self.FACTS)
        self.assertIsNone(case)
        self.assertIn('no deterministic content check', why)

    def test_env_sent_is_generatable_now_that_mail_is_simulated(self):
        # Mail simulators landed in E-3, so env_sent cleans like any other
        # content grader. (Drive graders are the next "no simulator" case.)
        raw = self._raw(graders=[{'type': 'env_sent', 'to': 'a@b.c'}],
                        expect_state={})
        case, why = clean_env_case(raw, self.TOOLS, self.FACTS)
        self.assertIsNotNone(case, why)


class ProveCaseTests(SimpleTestCase):
    FILES = {'notes.md': 'Q3 revenue is 412300.\n'}

    def _case(self, **kwargs):
        base = dict(
            name='c', goal='Report it.',
            reference='Correct answer: 412300',
            input_data={'__expect_state__': {'files': {'summary.md': 'Q3: 412300\n'}}},
            graders=[{'type': 'file_contains', 'path': 'summary.md',
                      'value': '412300'}],
        )
        base.update(kwargs)
        return base

    def test_ideal_passes_untouched_fails(self):
        self.assertEqual(
            async_to_sync(prove_case)({'files': self.FILES}, self._case()), (True, ''))

    def test_passes_with_nothing_done_is_unprovable(self):
        case = self._case(graders=[{'type': 'file_exists', 'path': 'notes.md'}])
        ok, reason = async_to_sync(prove_case)({'files': self.FILES}, case)
        self.assertFalse(ok)
        self.assertIn('nothing done', reason)

    def test_ideal_failing_its_own_checks_is_unprovable(self):
        case = self._case(graders=[{'type': 'file_contains', 'path': 'summary.md',
                                    'value': 'nope'}])
        ok, reason = async_to_sync(prove_case)({'files': self.FILES}, case)
        self.assertFalse(ok)
        self.assertIn('ideal outcome fails', reason)


# ---------------------------------------------------------------- pipeline


def _judge_replies(*replies):
    """Canned `_judge_call` answers, in pipeline order: completion-shaped."""
    calls = {'n': 0}

    async def fake(prompt, system, user_id, max_tokens):
        reply = replies[min(calls['n'], len(replies) - 1)]
        calls['n'] += 1
        text = reply() if callable(reply) else reply
        return SimpleNamespace(content=text, tokens=10, usage=None)

    return fake


class GenerateWorldTests(TestCase):
    def setUp(self):
        self.user = _user('gen')
        self.agent = _agent(self.user)

    def _replies(self, **kwargs):
        brief = kwargs.get('brief', 'Acme Tools, Q3 close.')
        facts = kwargs.get('facts', [
            {'key': 'q3', 'value': '412300', 'statement': 'Q3 is 412300.'},
            {'key': 'q2', 'value': '10100', 'statement': 'Order 2 is 10100.'},
            {'key': 'inv', 'value': 'INV-1043', 'statement': 'INV-1043 twice.'},
            {'key': 'lead', 'value': 'Priya', 'statement': 'Priya leads close.'},
            {'key': 'day', 'value': 'Friday', 'statement': 'Close day Friday.'},
        ])
        files = kwargs.get('files', {
            'notes.md': ('Q3 revenue is 412300.\nOrder 2 is 10100.\n'
                         'INV-1043 twice.\nPriya leads close.\nClose day Friday.\n'),
        })
        goal = kwargs.get('goal', 'Report Q3 revenue.')
        expected = kwargs.get('expected', '412300')
        return [
            json.dumps({'brief': brief, 'facts': facts}),
            json.dumps({'files': files}),
            json.dumps({'cases': [dict(
                name='Revenue?', category='normal', goal=goal, input_data={},
                facts_used=['q3'], expected=expected,
                expect_state={'files': {'summary.md': f'Q3: {expected}\n'}},
                graders=[{'type': 'file_contains', 'path': 'summary.md',
                          'value': expected}])]}),
            json.dumps({'answers': [{'id': 'c0', 'answer': expected}]}),
            json.dumps({'verdicts': [{'id': 'c0', 'agree': True,
                                      'reason': 'same value'}]}),
        ]

    def test_full_pipeline_offline(self):
        with patch('eval.generator._judge_call',
                   _judge_replies(*self._replies())):
            out = async_to_sync(generate_world)(
                self.agent, user_id=self.user.id, focus='close', cases=1)
        self.assertEqual(out['brief'], 'Acme Tools, Q3 close.')
        self.assertEqual(len(out['cases']), 1)
        self.assertEqual(out['rejected'], [])
        self.assertIn('412300', out['cases'][0]['reference'])

    def test_blind_disagreement_drops_the_case(self):
        replies = self._replies()
        replies[-1] = json.dumps({'verdicts': [{'id': 'c0', 'agree': False,
                                                'reason': 'no'}]})
        with patch('eval.generator._judge_call', _judge_replies(*replies)):
            out = async_to_sync(generate_world)(
                self.agent, user_id=self.user.id, cases=1)
        self.assertEqual(out['cases'], [])
        self.assertTrue(any('disagreed' in r for r in out['rejected']))

    def test_uncovered_facts_fail_the_world(self):
        replies = self._replies(files={'notes.md': 'nothing relevant here'})
        with patch('eval.generator._judge_call', _judge_replies(*replies)):
            with self.assertRaises(ValueError) as ctx:
                async_to_sync(generate_world)(self.agent, user_id=self.user.id)
        self.assertIn('planted facts', str(ctx.exception))

    def test_agent_with_nothing_to_simulate_is_refused(self):
        from eval.generator import WorldNotPossible

        agent = _agent(self.user, 'Bare', tool_grants={},
                       sandbox={'fileAccess': 'none'})
        with self.assertRaises(WorldNotPossible) as ctx:
            async_to_sync(generate_world)(agent, user_id=self.user.id)
        self.assertIn('file access', str(ctx.exception))

    def test_answers_are_matched_by_id_not_position(self):
        # The solver answers the case under the wrong id first and skips the
        # real one: under positional matching the verdict would land on c0.
        replies = self._replies()
        replies[-2] = json.dumps({'answers': [{'id': 'c9', 'answer': '412300'}]})
        with patch('eval.generator._judge_call', _judge_replies(*replies[:-1])):
            out = async_to_sync(generate_world)(
                self.agent, user_id=self.user.id, cases=1)
        self.assertEqual(out['cases'], [])
        self.assertTrue(any('disagreed' in r for r in out['rejected']))

    def test_web_only_agent_gets_a_world_without_files(self):
        agent = _agent(self.user, 'Researcher', tool_grants={'webSearch': True},
                       sandbox={'fileAccess': 'none'})
        page = {'url': 'https://acme.test/pricing', 'title': 'Pricing',
                'text': 'Pro plan is 412300 a year. INV-1043. Priya. Friday. 10100.'}
        replies = self._replies(files={})
        replies[1] = json.dumps({'files': {}, 'web': {
            'pages': [page], 'results': {'acme pricing': [page['url']]}}})
        replies[2] = json.dumps({'cases': [dict(
            name='Price?', category='normal', goal='What does Pro cost?',
            input_data={}, facts_used=['q3'], expected='412300',
            graders=[{'type': 'contains', 'value': '412300'}])]})
        with patch('eval.generator._judge_call', _judge_replies(*replies)):
            out = async_to_sync(generate_world)(agent, user_id=self.user.id, cases=1)
        self.assertEqual(out['fixtures']['files'], {})
        self.assertEqual(len(out['cases']), 1, out['rejected'])


# ---------------------------------------------------------------- review API


class WorldReviewAPITests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user('owner', 'owner@example.com', 'pw')
        self.client.force_authenticate(self.user)
        self.agent = SubAgent.objects.create(user=self.user, name='A')
        self.suite = EvalSuite.objects.create(
            user=self.user, name='S', subagent=self.agent)
        self.world = _world(self.suite, status='draft')
        self.case = EvalCase.objects.create(
            suite=self.suite, goal='g', graders=[{'type': 'no_error'}],
            is_active=False, tags=['generated', 'needs-review'],
            world_version=1)

    def test_case_cannot_be_accepted_before_its_world(self):
        url = reverse('eval:suite_review_drafts', args=[self.suite.id])
        response = self.client.post(url, {'accept': [self.case.id]}, format='json')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['accepted'], 0)
        self.assertEqual(response.data['refused'], [self.case.id])

    def test_accept_world_then_case(self):
        self.client.post(reverse('eval:world_accept', args=[self.world.id]))
        url = reverse('eval:suite_review_drafts', args=[self.suite.id])
        response = self.client.post(url, {'accept': [self.case.id]}, format='json')
        self.assertEqual(response.data['accepted'], 1)
        self.case.refresh_from_db()
        self.assertTrue(self.case.is_active)

    def test_suite_world_reports_live_and_draft(self):
        response = self.client.get(reverse('eval:suite_world', args=[self.suite.id]))
        self.assertIsNone(response.data['live'])
        self.assertEqual(response.data['draft']['version'], 1)
        self.client.post(reverse('eval:world_accept', args=[self.world.id]))
        response = self.client.get(reverse('eval:suite_world', args=[self.suite.id]))
        self.assertEqual(response.data['live']['version'], 1)
        self.assertEqual(response.data['live']['brief'], self.world.brief)

    def test_accepted_worlds_cannot_be_deleted(self):
        self.client.post(reverse('eval:world_accept', args=[self.world.id]))
        response = self.client.delete(reverse('eval:world_detail', args=[self.world.id]))
        self.assertEqual(response.status_code, 400)

    def test_new_cases_stamp_the_live_version(self):
        self.client.post(reverse('eval:world_accept', args=[self.world.id]))
        response = self.client.post(
            reverse('eval:case_list', args=[self.suite.id]),
            {'goal': 'hand-written', 'graders': [{'type': 'no_error'}]},
            format='json')
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data['world_version'], 1)

    def test_generate_refuses_suites_with_a_live_world(self):
        self.client.post(reverse('eval:world_accept', args=[self.world.id]))
        response = self.client.post(
            reverse('eval:suite_generate', args=[self.suite.id]), {}, format='json')
        self.assertEqual(response.status_code, 400)
        self.assertIn('world', response.data['error'])
