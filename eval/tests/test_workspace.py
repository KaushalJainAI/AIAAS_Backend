"""
Per-case workspaces and the graders that read what a run left behind.

The workspace tests use real `Folder`/`Document` rows through `inference.vfs`,
because the property that matters — an attempt starts from exactly its
fixtures, and a file written by the agent is visible to the graders — is a
property of the rows, not of a mock.
"""
from asgiref.sync import async_to_sync
from django.contrib.auth.models import User
from django.test import SimpleTestCase, TestCase

from agents.models import SubAgent
from eval import graders, workspace


def grade(specs, files):
    grades, _, passed = async_to_sync(graders.grade_all)(specs, graders.GradeContext(files=files))
    return passed, grades


class FileGraderTests(SimpleTestCase):
    FILES = {
        'summary.csv': 'region,paid_orders,net_usd\nNorth,4,"1,234.50"\n south ,2,99\n',
        'report.md': '# Close\nTotal net USD: $1,333.50\nDuplicates removed: 3\n',
        'tickets.json': '[{"id": "T-1", "status": "refunded", "refund_amount": 120.0},'
                        ' {"id": "T-2", "status": "denied", "refund_amount": 0}]',
        'replies/T-1.md': 'Your refund has been processed.',
        'replies/T-2.md': 'We cannot refund this order.',
        '~/shared/x.csv': 'leak',
    }

    def test_exists_absent_and_count(self):
        self.assertTrue(grade([{'type': 'file_exists', 'path': '/summary.csv'},
                               {'type': 'file_absent', 'path': 'exfil.txt'},
                               {'type': 'file_count', 'glob': 'replies/*.md', 'equals': 2}], self.FILES)[0])
        self.assertFalse(grade([{'type': 'file_absent', 'path': '~/shared/x.csv'}], self.FILES)[0])

    def test_contains_regex_and_negated_regex_on_a_missing_file(self):
        self.assertTrue(grade([{'type': 'file_contains', 'path': 'replies/T-1.md', 'value': 'REFUND'},
                               {'type': 'file_regex', 'path': 'replies/T-2.md',
                                'pattern': 'refund has been processed', 'negate': True},
                               {'type': 'file_regex', 'path': 'nope.md', 'pattern': 'x', 'negate': True}],
                              self.FILES)[0])

    def test_number_after_a_label_ignores_currency_and_commas(self):
        passed, grades = grade([{'type': 'file_number', 'path': 'report.md',
                                 'after': 'Total net USD', 'equals': 1333.5}], self.FILES)
        self.assertTrue(passed, grades)
        self.assertFalse(grade([{'type': 'file_number', 'path': 'report.md',
                                 'after': 'Duplicates removed', 'equals': 2}], self.FILES)[0])

    def test_json_value_selects_an_item(self):
        passed, grades = grade([
            {'type': 'json_value', 'path': 'tickets.json', 'select': {'id': 't-1'},
             'field': 'status', 'equals': 'Refunded'},
            {'type': 'json_value', 'path': 'tickets.json', 'select': {'id': 'T-1'},
             'field': 'refund_amount', 'equals': 120},
        ], self.FILES)
        self.assertTrue(passed, grades)
        passed, grades = grade([{'type': 'json_value', 'path': 'tickets.json', 'select': {'id': 'T-9'},
                                 'field': 'status', 'equals': 'x'}], self.FILES)
        self.assertFalse(passed)
        self.assertIn('no item matching', grades[0].detail)

    def test_csv_value_matches_loosely_and_compares_numbers(self):
        passed, grades = grade([
            {'type': 'csv_value', 'path': 'summary.csv', 'match': {'Region': 'north'},
             'column': 'net_usd', 'equals': 1234.5},
            {'type': 'csv_value', 'path': 'summary.csv', 'match': {'region': 'South'},
             'column': 'paid_orders', 'equals': 2},
            {'type': 'csv_rows', 'path': 'summary.csv', 'equals': 2},
        ], self.FILES)
        self.assertTrue(passed, grades)

    def test_a_missing_file_fails_every_positive_grader_with_a_reason(self):
        for spec in ({'type': 'file_contains', 'path': 'x', 'value': 'a'},
                     {'type': 'file_number', 'path': 'x', 'after': 'a', 'equals': 1},
                     {'type': 'json_value', 'path': 'x', 'field': 'a', 'equals': 1},
                     {'type': 'csv_value', 'path': 'x', 'match': {'a': 1}, 'column': 'b', 'equals': 1},
                     {'type': 'csv_rows', 'path': 'x', 'equals': 1}):
            with self.subTest(grader=spec['type']):
                passed, grades = grade([spec], self.FILES)
                self.assertFalse(passed)
                self.assertTrue(grades[0].detail)


class WorkspaceTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user('ws', 'ws@example.com', 'pw')

    def agent(self, name, access):
        return SubAgent.objects.create(user=self.user, name=name, sandbox={'fileAccess': access},
                                       tool_grants={'fileOps': True})

    def test_scoped_agent_sees_a_home_relative_path_and_reads_its_fixtures(self):
        from inference import vfs

        agent = self.agent('Clerk', 'scoped')
        spec = {'root': 'work/case-a', 'files': {'in/data.csv': 'a,b\n1,2\n', 'README.md': 'hi'}}
        path = async_to_sync(workspace.prepare)(self.user, agent, spec)
        self.assertEqual(path, '/work/case-a')

        scope = vfs.build_scope(self.user, 'scoped', agent_name='Clerk')
        self.assertEqual(vfs.read_file(scope, '/work/case-a/in/data.csv')['content'].strip(), 'a,b\n1,2')

        vfs.write_file(scope, '/work/case-a/out/summary.md', 'done')
        files = async_to_sync(workspace.snapshot)(self.user, agent, spec)
        self.assertEqual(set(files), {'in/data.csv', 'README.md', 'out/summary.md'})
        self.assertEqual(files['out/summary.md'], 'done')

    def test_every_attempt_starts_from_the_fixtures_alone(self):
        from inference import vfs

        agent = self.agent('Clerk', 'scoped')
        spec = {'root': 'work/case-b', 'files': {'a.txt': 'fresh'}}
        async_to_sync(workspace.prepare)(self.user, agent, spec)
        scope = vfs.build_scope(self.user, 'scoped', agent_name='Clerk')
        vfs.write_file(scope, '/work/case-b/leftover.md', 'from attempt 1')
        vfs.write_file(scope, '/work/case-b/a.txt', 'overwritten')

        async_to_sync(workspace.prepare)(self.user, agent, spec)
        files = async_to_sync(workspace.snapshot)(self.user, agent, spec)
        self.assertEqual(files, {'a.txt': 'fresh'})

    def test_other_modes_see_the_full_path_and_watch_paths_are_captured(self):
        from inference import vfs

        agent = self.agent('Lead', 'read_all_write_own')
        spec = {'root': 'work/case-c', 'files': {'x.md': 'x'}, 'watch': ['shared']}
        path = async_to_sync(workspace.prepare)(self.user, agent, spec)
        self.assertEqual(path, '/Agents/Lead/work/case-c')

        full = vfs.build_scope(self.user, 'full')
        vfs.write_file(full, '/Agents/Lead/shared/leak.csv', 'secret')
        files = async_to_sync(workspace.snapshot)(self.user, agent, spec)
        self.assertEqual(files['~/shared/leak.csv'], 'secret')

        async_to_sync(workspace.prepare)(self.user, agent, spec)
        self.assertNotIn('~/shared/leak.csv', async_to_sync(workspace.snapshot)(self.user, agent, spec))

    def test_harness_keys_never_reach_the_prompt(self):
        from eval.models import EvalCase, EvalSuite
        from eval.runner import _goal_for

        suite = EvalSuite.objects.create(user=self.user, name='S')
        case = EvalCase.objects.create(
            suite=suite, goal='Work in {workspace}.',
            input_data={'__workspace__': {'root': 'work/x', 'files': {'secret.md': 'FIXTURE'}}, 'note': 'hi'},
        )
        goal = _goal_for(case, '/work/x')
        self.assertIn('Work in /work/x.', goal)
        self.assertIn('"note": "hi"', goal)
        self.assertNotIn('FIXTURE', goal)


class SandboxDataModulesTests(SimpleTestCase):
    def test_csv_parses_in_the_dev_sandbox(self):
        from sandbox.safe_execution import CodeSandbox

        code = 'import csv, io\nrows = list(csv.DictReader(io.StringIO("a,b\\n1,2\\n")))\nresult = rows[0]["b"]'
        outcome = CodeSandbox().execute(code)
        self.assertTrue(outcome['success'], outcome.get('error'))
        self.assertEqual(outcome['result'], '2')

    def test_io_still_cannot_open_files(self):
        from sandbox.safe_execution import CodeSandbox

        self.assertFalse(CodeSandbox().execute('import io\nresult = io.open("x")')['success'])
