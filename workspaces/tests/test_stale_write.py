"""
C2 — the stale-write guard: a run cannot edit what changed since it read it.

The hash comparison is the real correctness guarantee; notices (C3) are only
a courtesy. These tests drive the `ws_*` tools directly with a stubbed
workspace engine: one fake file, two threads, and the refusal in between.
"""
import hashlib
import json

from asgiref.sync import async_to_sync
from django.contrib.auth.models import User
from django.test import TransactionTestCase

from agents.models import SubAgent
from chat.tools import code as code_tools
from logs.models import ExecutionLog
from workspaces import reads
from workspaces.models import CodeProject


class _FakeWS:
    """One file's worth of workspace engine, keyed by full path."""

    def __init__(self):
        self.files: dict[str, bytes] = {}


def _context(user_id, thread, write_paths=None, claims=(), execution_id='',
             label='Implementer #1', task_id='t1', commands=None):
    return {
        'user_id': user_id,
        'session_id': thread,
        'turn_id': 'turn-1',
        'write_paths': None if write_paths is None else tuple(write_paths),
        'command_scope': None if commands is None else tuple(commands),
        'task_claims': tuple(claims),
        'task_id': task_id,
        'worker_label': label,
        'execution_id': execution_id,
    }


class StaleWriteTests(TransactionTestCase):
    """TransactionTestCase + async_to_sync: `TestCase` holds SQLite open in a
    transaction that the tools' `sync_to_async` threads then find locked."""

    def run_tool(self, name, args, ctx):
        return json.loads(async_to_sync(getattr(code_tools, name))(args, ctx))
    def setUp(self):
        reads.clear()
        self.user = User.objects.create_user(username='coder', password='pw')
        self.agent = SubAgent.objects.create(user=self.user, name='Impl')
        self.project = CodeProject.objects.create(
            user=self.user, name='api', workspace_path='/home/user/projects/api',
            commands={'test': 'pytest -q'})
        self.log_a = ExecutionLog.objects.create(
            subagent=self.agent, user=self.user, status='running')
        self.log_b = ExecutionLog.objects.create(
            subagent=self.agent, user=self.user, status='running')
        self.state = _FakeWS()
        self.state.files['/home/user/projects/api/src/a.ts'] = b'const x = 1;\n'

        async def _read(ws, path):
            if path not in self.state.files:
                from workspaces.engine import WorkspaceError

                raise WorkspaceError('No such file.')
            return self.state.files[path]

        async def _write(ws, path, data):
            self.state.files[path] = bytes(data)

        async def _exec(ws, cmd, cwd='', env=None, timeout=120, stdin=None):
            return {'exit_code': 0, 'stdout': 'ok', 'stderr': ''}

        def _ensure(user):
            return self.state

        import workspaces.engine as engine

        self._orig = (engine.read, engine.write, engine.exec, engine.ensure)
        engine.read, engine.write, engine.exec, engine.ensure = (
            _read, _write, _exec, _ensure)

    def tearDown(self):
        import workspaces.engine as engine

        engine.read, engine.write, engine.exec, engine.ensure = self._orig
        reads.clear()

    def _ctx_a(self, **kw):
        base = {'execution_id': str(self.log_a.execution_id)}
        base.update(kw)
        return _context(self.user.id, 'thread-a', **base)

    def _ctx_b(self, **kw):
        base = {'execution_id': str(self.log_b.execution_id)}
        base.update(kw)
        return _context(self.user.id, 'thread-b', label='Implementer #2',
                        task_id='t2', **base)

    def test_read_returns_a_hash_and_edit_after_reread_succeeds(self):
        body = self.run_tool(
            'ws_read', {'path': 'src/a.ts', 'project': 'api'}, self._ctx_a())
        self.assertEqual(
            body['sha256'], hashlib.sha256(b'const x = 1;\n').hexdigest())
        out = self.run_tool('ws_edit',
            {'path': 'src/a.ts', 'project': 'api',
             'old': 'const x = 1;', 'new': 'const x = 2;'}, self._ctx_a())
        self.assertTrue(out.get('edited'))

    def test_a_stale_edit_is_refused_and_a_reread_unblocks(self):
        self.run_tool(
            'ws_read', {'path': 'src/a.ts', 'project': 'api'}, self._ctx_a())
        # Another worker changes the file first.
        self.run_tool(
            'ws_read', {'path': 'src/a.ts', 'project': 'api'}, self._ctx_b())
        out_b = self.run_tool('ws_edit',
            {'path': 'src/a.ts', 'project': 'api',
             'old': 'const x = 1;', 'new': 'const x = 9;'}, self._ctx_b())
        self.assertTrue(out_b.get('edited'))
        # A's view is now stale: refused, naming the fix.
        out_a = self.run_tool('ws_edit',
            {'path': 'src/a.ts', 'project': 'api',
             'old': 'const x = 1;', 'new': 'const x = 3;'}, self._ctx_a())
        self.assertIn('changed since you read it', out_a.get('error', ''))
        self.assertIn('re-read', out_a.get('error', ''))
        # Nothing was written by the refused edit.
        self.assertIn(b'const x = 9;', self.state.files['/home/user/projects/api/src/a.ts'])
        # B finishes and releases its lease; re-reading unblocks the re-based
        # edit. (Without the release the retry would — correctly — meet B's
        # lease instead of the stale guard.)
        from workspaces import leases

        leases.release_holder(self.log_b.id)
        self.run_tool(
            'ws_read', {'path': 'src/a.ts', 'project': 'api'}, self._ctx_a())
        out_retry = self.run_tool('ws_edit',
            {'path': 'src/a.ts', 'project': 'api',
             'old': 'const x = 9;', 'new': 'const x = 3;'}, self._ctx_a())
        self.assertTrue(out_retry.get('edited'))

    def test_a_write_outside_the_claims_is_refused_and_writes_nothing(self):
        ctx = self._ctx_a(claims=['src/a.ts'])
        out = self.run_tool('ws_edit',
            {'path': 'src/other.ts', 'project': 'api',
             'old': 'x', 'new': 'y'}, ctx)
        self.assertIn('claims', out.get('error', ''))

    def test_a_write_outside_write_paths_is_refused(self):
        ctx = self._ctx_a(write_paths=['src/allowed/**'], claims=[])
        out = self.run_tool('ws_write',
            {'path': 'src/elsewhere/new.ts', 'project': 'api',
             'content': 'hi'}, ctx)
        self.assertIn('writePaths', out.get('error', ''))

    def test_command_scope_refuses_what_it_does_not_name(self):
        ctx = self._ctx_a(commands=['test'])
        out = self.run_tool('ws_run',
            {'cmd': 'curl https://example.com', 'project': 'api'}, ctx)
        self.assertIn('commandScope', out.get('error', ''))
        ok = self.run_tool('ws_run',
            {'cmd': 'pytest -q', 'project': 'api'}, ctx)
        self.assertEqual(ok.get('exit_code'), 0)

    def test_recorded_change_carries_hashes(self):
        self.run_tool(
            'ws_read', {'path': 'src/a.ts', 'project': 'api'}, self._ctx_a())
        self.run_tool('ws_edit',
            {'path': 'src/a.ts', 'project': 'api',
             'old': 'const x = 1;', 'new': 'const x = 2;'}, self._ctx_a())
        from workspaces.models import CodeChange

        row = CodeChange.objects.filter(run=self.log_a).order_by('-id').first()
        self.assertIsNotNone(row)
        self.assertTrue(row.before_hash)
        self.assertTrue(row.after_hash)
        self.assertNotEqual(row.before_hash, row.after_hash)
