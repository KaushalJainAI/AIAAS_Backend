"""
Tests for `eval.api` — the surface another app is allowed to depend on.

These are the tests that fail if the app stops being importable from outside:
a module-level import of a sibling app (which would make an import cycle
possible), a public name quietly renamed, or `eval/__init__.py` growing an
import that touches the ORM before the app registry is ready.
"""
import ast
import pathlib

from asgiref.sync import async_to_sync
from django.contrib.auth.models import User
from django.test import SimpleTestCase, TestCase

from agents.agent.runtime import AgentRun
from agents.models import SubAgent
from eval import api
from eval.models import EvalCase, EvalRun, EvalSuite
from logs.models import ExecutionLog

EVAL_DIR = pathlib.Path(__file__).resolve().parent.parent
#: The apps `eval` reaches into. Every one of these must be imported *inside* a
#: function, never at module scope.
SIBLING_APPS = {'agents', 'logs', 'llm', 'notifications', 'chat', 'inference',
                'credentials', 'mcp_integration', 'skills'}
#: Where Django's own machinery makes a module-level model import unavoidable.
ORM_BOUND = {'models.py', 'admin.py', 'serializers.py', 'views.py', 'urls.py',
             'queries.py', 'api.py'}


class ImportabilityTests(SimpleTestCase):
    """The app has to be importable from anywhere without an import cycle."""

    def test_no_module_level_imports_of_sibling_apps(self):
        # A module-level `from agents...` here would mean `agents` importing
        # `eval` could deadlock on a cycle. Every cross-app import in this
        # package is function-local, and this is what keeps it that way.
        offenders = []
        for path in sorted(EVAL_DIR.glob('*.py')):
            tree = ast.parse(path.read_text(encoding='utf-8'))
            for node in tree.body:  # top level only — nested imports are fine
                if isinstance(node, ast.Import):
                    names = [a.name.split('.')[0] for a in node.names]
                elif isinstance(node, ast.ImportFrom):
                    names = [(node.module or '').split('.')[0]]
                else:
                    continue
                for name in names:
                    if name in SIBLING_APPS:
                        offenders.append(f'{path.name}: {name}')
        self.assertEqual(offenders, [])

    def test_init_stays_empty(self):
        # `INSTALLED_APPS` imports the package before the app registry is
        # ready, so anything here touching eval.models raises
        # AppRegistryNotReady on boot. The façade lives in eval/api.py.
        init = (EVAL_DIR / '__init__.py').read_text(encoding='utf-8')
        self.assertEqual(init.strip(), '')

    def test_the_pure_layer_needs_no_orm(self):
        # `graders` and `supervision`'s policy half must not import models at
        # module scope either, or "score this text" would drag in the database.
        for name in ('graders.py', 'supervision.py'):
            tree = ast.parse((EVAL_DIR / name).read_text(encoding='utf-8'))
            modules = [
                (node.module or '') for node in tree.body
                if isinstance(node, ast.ImportFrom)
            ]
            self.assertNotIn('.models', modules, name)

    def test_every_advertised_name_exists(self):
        missing = [name for name in api.__all__ if not hasattr(api, name)]
        self.assertEqual(missing, [])

    def test_orm_bound_modules_are_the_only_ones_importing_models(self):
        for path in sorted(EVAL_DIR.glob('*.py')):
            tree = ast.parse(path.read_text(encoding='utf-8'))
            imports_models = any(
                isinstance(node, ast.ImportFrom) and (node.module or '').endswith('models')
                for node in tree.body
            )
            if imports_models:
                self.assertIn(path.name, ORM_BOUND, f'{path.name} imports models')


class PureGradingTests(SimpleTestCase):
    """`grade_answer` is the entry point for a caller with no eval rows."""

    def test_grade_answer_returns_plain_json(self):
        out = async_to_sync(api.grade_answer)(
            'The capital is Paris.', [{'type': 'contains', 'value': 'paris'}],
        )
        self.assertEqual(out['score'], 1.0)
        self.assertTrue(out['passed'])
        # Plain dicts: a caller persisting this must not have to import our
        # dataclass to read it.
        self.assertIsInstance(out['grades'][0], dict)
        self.assertEqual(out['grades'][0]['type'], 'contains')

    def test_grade_answer_accepts_any_context_field(self):
        out = async_to_sync(api.grade_answer)(
            'done', [{'type': 'tool_used', 'tool': 'web_search'}],
            tool_trace=[{'tool': 'web_search'}],
        )
        self.assertTrue(out['passed'])

    def test_nothing_to_decide_stays_undecided(self):
        out = async_to_sync(api.grade_answer)('anything', [])
        self.assertIsNone(out['passed'])

    def test_validate_graders_is_reusable(self):
        with self.assertRaises(api.GraderError):
            api.validate_graders([{'type': 'nope'}])

    def test_list_graders_is_pure(self):
        self.assertIn('contains', {g['type'] for g in api.list_graders()})


class GradeExistingRunTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user('api', 'api@example.com', 'pw')
        self.agent = SubAgent.objects.create(user=self.user, name='Geo')

    def test_an_execution_can_be_scored_after_the_fact(self):
        # The point of this entry point: score a run started by chat, a trigger
        # or a delegation, with no suite and no case involved.
        execution = ExecutionLog.objects.create(
            subagent=self.agent, user=self.user, status='completed',
            input_data={'goal': 'Capital of France?'},
            output_data={'answer': 'It is Paris.',
                         'tool_trace': [{'tool': 'web_search'}]},
            tokens_used=120, duration_ms=800,
        )
        out = async_to_sync(api.grade_execution)(execution, [
            {'type': 'contains', 'value': 'paris'},
            {'type': 'tool_used', 'tool': 'web_search'},
            {'type': 'max_tokens', 'value': 500},
            {'type': 'no_error'},
        ])
        self.assertTrue(out['passed'])
        self.assertEqual(len(out['grades']), 4)

    def test_a_failed_run_is_graded_as_an_error_condition(self):
        execution = ExecutionLog.objects.create(
            subagent=self.agent, user=self.user, status='failed',
            output_data={}, error_message='provider timed out',
        )
        out = async_to_sync(api.grade_execution)(execution, [{'type': 'no_error'}])
        self.assertFalse(out['passed'])
        self.assertIn('timed out', out['grades'][0]['detail'])

    def test_grading_an_execution_persists_nothing(self):
        execution = ExecutionLog.objects.create(
            subagent=self.agent, user=self.user, status='completed',
            output_data={'answer': 'Paris'},
        )
        async_to_sync(api.grade_execution)(
            execution, [{'type': 'contains', 'value': 'paris'}],
        )
        # The caller decides whether a verdict is worth keeping; a sweep is
        # what persists.
        self.assertEqual(EvalRun.objects.count(), 0)


class AwaitableSweepTests(TestCase):
    """`run_suite_now` is the seam a command, task or test needs."""

    def setUp(self):
        self.user = User.objects.create_user('sweep', 's@example.com', 'pw')
        self.agent = SubAgent.objects.create(user=self.user, name='Geo')
        self.suite = EvalSuite.objects.create(
            user=self.user, name='Capitals', subagent=self.agent,
            supervision='none', pass_threshold=0.5,
        )
        EvalCase.objects.create(
            suite=self.suite, goal='Capital of France?',
            graders=[{'type': 'contains', 'value': 'paris'}],
        )

    def test_a_sweep_can_be_awaited_without_touching_privates(self):
        from unittest.mock import patch

        async def fake(agent, goal, **kwargs):
            return AgentRun(
                execution_id='00000000-0000-0000-0000-000000000000',
                answer='Paris', thinking='', tool_trace=[], tokens=10,
                awaiting_approval=False, unserved_grants=(), duration_ms=5,
            )

        with patch('agents.agent.runtime.run_agent', fake):
            run = async_to_sync(api.run_suite_now)(self.suite, self.agent, self.user)

        # Already settled when it comes back — no polling, no reload.
        self.assertEqual(run.status, 'completed')
        self.assertEqual(run.score, 1.0)
        self.assertTrue(run.passed)
