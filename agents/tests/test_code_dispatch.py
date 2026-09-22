"""
C4 — the lead can start, watch, steer and stop workers.

`invoke_subagent` blocks until every worker ends, so a lead that uses it has
no turn in which to react. `start_tasks` returns handles immediately and
`wait_tasks` returns on events; `stop_task` releases leases and keeps changes
for the lead to keep or revert.
"""
import asyncio
import json
from unittest.mock import patch

from asgiref.sync import async_to_sync
from django.contrib.auth.models import User
from django.test import TransactionTestCase

from agents.agent import tasks as task_registry
from agents.models import SubAgent
from chat.tools import tasks as dispatch
from workspaces.models import CodeProject


def _ctx(user, thread='lead-thread', **kw):
    base = {
        'user_id': user.id,
        'session_id': thread,
        'turn_id': 'turn-1',
        'depth': 0,
        'delegation_scope': None,
        'code_projects': None,
        'write_paths': None,
        'command_scope': None,
        'tool_permissions': {},
        'file_scope': None,
        'deadline': None,
    }
    base.update(kw)
    return base


class FakeRun:
    def __init__(self, answer='did the thing', tokens=10, awaiting_approval=False):
        self.answer = answer
        self.tokens = tokens
        self.awaiting_approval = awaiting_approval


class DispatchTests(TransactionTestCase):
    def setUp(self):
        task_registry.clear()
        self.user = User.objects.create_user(username='lead', password='pw')
        self.project = CodeProject.objects.create(
            user=self.user, name='api', workspace_path='/home/user/projects/api')
        self.impl = SubAgent.objects.create(
            user=self.user, name='Impl', template_slug='code-implementer',
            tool_grants={'shell': True, 'subAgents': False},
            guardrails={'autonomy': 'auto', 'spendCapRupees': 600},
            agent_context={},
            allow_unattended=True)
        self.ctx = _ctx(self.user)

    def tearDown(self):
        task_registry.clear()

    def start(self, tasks, **kw):
        return json.loads(async_to_sync(dispatch.start_tasks)(
            {'tasks': tasks, **kw}, self.ctx))

    def task(self, task_id='t1', **kw):
        base = {'id': task_id, 'title': f'Task {task_id}',
                'agent': 'code-implementer',
                'instructions': 'do it', 'claims': ['src/a.ts'],
                'project': 'api'}
        base.update(kw)
        return base

    def test_two_tasks_claiming_the_same_file_do_not_both_start(self):
        async def _fast(*a, **k):
            return FakeRun()

        with patch('agents.agent.runtime.run_agent', side_effect=_fast):
            # The second task never reaches the stub: the batch overlap check
            # refuses it first.
            out = self.start([self.task('t1'), self.task('t2')])
        self.assertEqual(len(out['started']), 1)
        self.assertEqual(len(out['refused']), 1)
        self.assertIn('t1', out['refused'][0]['refused'])
        self.assertEqual(out['started'][0]['task_id'], 't1')

    def test_a_task_whose_dependencies_are_unfinished_names_them(self):
        out = self.start([self.task('t2', depends_on=['t1'])])
        self.assertEqual(out['started'], [])
        self.assertIn('t1', out['refused'][0]['refused'])

    def test_a_writing_task_without_claims_is_refused(self):
        out = self.start([self.task('t1', claims=[])])
        self.assertEqual(out['started'], [])
        self.assertIn('claims', out['refused'][0]['refused'])

    def test_an_unknown_agent_is_refused_with_the_fix(self):
        out = self.start([self.task('t1', agent='code-teleporter')])
        self.assertEqual(out['started'], [])
        self.assertIn('code pack', out['refused'][0]['refused'])

    def test_parallelism_is_capped_at_three(self):
        # Slow workers that stay live for the whole scenario: the first three
        # start, the fourth and fifth are refused with the cap named. (Instant
        # workers would finish between launches and never trip the cap — which
        # is correct: it bounds how many run *at once*.)
        async def scenario():
            release_evt = asyncio.Event()

            async def _slow(*a, **k):
                await release_evt.wait()
                return FakeRun()

            with patch('agents.agent.runtime.run_agent', side_effect=_slow):
                tasks = [self.task(f't{i}', claims=[f'src/f{i}.ts'])
                         for i in range(5)]
                out = json.loads(await dispatch.start_tasks(
                    {'tasks': tasks}, self.ctx))
                release_evt.set()
                await dispatch.wait_tasks(
                    {'until': 'all', 'timeout_s': 30}, self.ctx)
                return out

        out = async_to_sync(scenario)()
        self.assertEqual(len(out['started']), 3, out)
        self.assertEqual(len(out['refused']), 2)
        self.assertIn('3 workers', out['refused'][0]['refused'])

    def test_start_then_wait_all_reports_done(self):
        async def _fast(*a, **k):
            return FakeRun(answer='patched src/a.ts')

        with patch('agents.agent.runtime.run_agent', side_effect=_fast):
            out = self.start([self.task('t1')])
            self.assertEqual(len(out['started']), 1)
            waited = json.loads(async_to_sync(dispatch.wait_tasks)(
                {'until': 'all', 'timeout_s': 30}, self.ctx))
        self.assertTrue(any('done' in e for e in waited['events']),
                        waited)
        self.assertIn('t1:done', waited['progress'])

    def test_stop_releases_the_lease_and_keeps_the_record(self):
        # One loop for the whole scenario (the LiveTaskTests pattern): the
        # worker, the stop and the assertions share it, so the live task is
        # found by name exactly as in production.
        from workspaces import leases as _leases

        async def scenario():
            from agents.agent.runtime import _close_log

            started_evt = asyncio.Event()
            finished_evt = asyncio.Event()

            async def _slow(*a, **k):
                log = k.get('log')
                started_evt.set()
                try:
                    await finished_evt.wait()
                except asyncio.CancelledError:
                    # What the real `run_agent` does on its cancel path.
                    await _close_log(log, status='cancelled', result={},
                                     tokens=0, error='Stopped.')
                    raise
                return FakeRun()

            with patch('agents.agent.runtime.run_agent', side_effect=_slow):
                from asgiref.sync import sync_to_async as _to_sync

                out = json.loads(await dispatch.start_tasks(
                    {'tasks': [self.task('t1')]}, self.ctx))
                handle = out['started'][0]['handle']
                await started_evt.wait()
                live = await _to_sync(
                    lambda: len(_leases.live_leases(self.project.id)))()
                assert live == 1, live
                stopped = json.loads(await dispatch.stop_task(
                    {'handle': handle, 'reason': 'wrong direction'}, self.ctx))
                return handle, stopped

        handle, stopped = async_to_sync(scenario)()
        self.assertEqual(stopped.get('stopped'), handle, stopped)
        # The wrapper was cancelled; the record stays for keep-or-revert.
        record = task_registry.get('lead-thread', handle)
        self.assertIsNotNone(record)
        self.assertEqual(record.status, 'cancelled')

    def test_status_names_leases_and_result(self):
        async def _fast(*a, **k):
            return FakeRun(answer='ok')

        with patch('agents.agent.runtime.run_agent', side_effect=_fast):
            out = self.start([self.task('t1')])
            handle = out['started'][0]['handle']
            async_to_sync(dispatch.wait_tasks)(
                {'until': 'all', 'timeout_s': 30}, self.ctx)
            status = json.loads(async_to_sync(dispatch.task_status)(
                {'handle': handle}, self.ctx))
        self.assertEqual(status['tasks'][0]['status'], 'done')
        self.assertIn('ok', status['tasks'][0]['answer_tail'])

    def test_revert_restores_what_the_worker_changed(self):
        import hashlib

        from workspaces.models import CodeChange

        async def scenario():
            async def _fast(*a, **k):
                return FakeRun(answer='ok')

            with patch('agents.agent.runtime.run_agent', side_effect=_fast):
                out = json.loads(await dispatch.start_tasks(
                    {'tasks': [self.task('t1')]}, self.ctx))
                handle = out['started'][0]['handle']
                await dispatch.wait_tasks(
                    {'until': 'all', 'timeout_s': 30}, self.ctx)

            from logs.models import ExecutionLog

            from asgiref.sync import sync_to_async as _to_sync

            log = await _to_sync(ExecutionLog.objects.get)(
                execution_id=out['started'][0]['execution_id'])
            body = b'const x = 2;\n'
            await _to_sync(CodeChange.objects.create)(
                run=log, path='src/a.ts',
                before_hash=hashlib.sha256(b'const x = 1;\n').hexdigest(),
                after_hash=hashlib.sha256(body).hexdigest(),
                diff='edit: src/a.ts')

            ran = []

            async def _read(ws, path):
                return body

            async def _exec(ws, cmd, cwd='', env=None, timeout=60, stdin=None):
                ran.append(cmd)
                return {'exit_code': 0, 'stdout': '', 'stderr': ''}

            def _ensure(user):
                return object()

            with patch('workspaces.engine.read', _read), \
                 patch('workspaces.engine.exec', _exec), \
                 patch('workspaces.engine.ensure', _ensure):
                result = json.loads(await dispatch.revert_task(
                    {'handle': handle}, self.ctx))
            return result, ran

        result, ran = async_to_sync(scenario)()
        self.assertEqual(result['reverted'], ['src/a.ts'], result)
        self.assertEqual(result['refused'], [])
        self.assertTrue(any('git checkout' in c for c in ran), ran)

    def test_revert_refuses_a_file_changed_since(self):
        import hashlib

        from workspaces.models import CodeChange

        async def scenario():
            async def _fast(*a, **k):
                return FakeRun(answer='ok')

            with patch('agents.agent.runtime.run_agent', side_effect=_fast):
                out = json.loads(await dispatch.start_tasks(
                    {'tasks': [self.task('t1')]}, self.ctx))
                handle = out['started'][0]['handle']
                await dispatch.wait_tasks(
                    {'until': 'all', 'timeout_s': 30}, self.ctx)

            from logs.models import ExecutionLog

            from asgiref.sync import sync_to_async as _to_sync

            log = await _to_sync(ExecutionLog.objects.get)(
                execution_id=out['started'][0]['execution_id'])
            await _to_sync(CodeChange.objects.create)(
                run=log, path='src/a.ts',
                before_hash=hashlib.sha256(b'const x = 1;\n').hexdigest(),
                after_hash=hashlib.sha256(b'const x = 2;\n').hexdigest(),
                diff='edit: src/a.ts')

            async def _read(ws, path):
                # Someone else rewrote it after the worker.
                return b'const x = 99;\n'

            async def _exec(ws, cmd, cwd='', env=None, timeout=60, stdin=None):
                raise AssertionError(f'should not run anything, ran {cmd}')

            def _ensure(user):
                return object()

            with patch('workspaces.engine.read', _read), \
                 patch('workspaces.engine.exec', _exec), \
                 patch('workspaces.engine.ensure', _ensure):
                result = json.loads(await dispatch.revert_task(
                    {'handle': handle}, self.ctx))
            return result

        result = async_to_sync(scenario)()
        self.assertEqual(result['reverted'], [])
        self.assertEqual(len(result['refused']), 1)
        self.assertIn('someone else', result['refused'][0])

    def test_a_lead_with_two_workers_emits_the_frames_a_client_receives(self):
        """The e2e the unit tests cannot replace: drive a lead with two stub
        workers and assert on the frames the client consumes — the running
        lane, both terminal lanes, and the lock list clearing.

        The todo panel shipped invisible once while six suites were green,
        because each covered one hop. This covers the chain."""
        from chat.turn.events import Event

        async def scenario():
            frames: list[tuple[str, dict]] = []

            async def _sink(event, payload):
                frames.append((str(event), dict(payload)))

            async def _fast(*a, **k):
                return FakeRun(answer='ok')

            ctx = dict(self.ctx, sink=_sink)
            with patch('agents.agent.runtime.run_agent', side_effect=_fast):
                out = json.loads(await dispatch.start_tasks(
                    {'tasks': [self.task('t1'), self.task('t2',
                                                           claims=['src/b.ts'])]}, ctx))
                self.assertEqual(len(out['started']), 2, out)
                await dispatch.wait_tasks(
                    {'until': 'all', 'timeout_s': 30}, ctx)
            return frames

        frames = async_to_sync(scenario)()
        by_type: dict[str, list[dict]] = {}
        for name, payload in frames:
            by_type.setdefault(name, []).append(payload)

        running = [p for p in by_type.get('task_update', [])
                   if p.get('status') == 'running']
        done = [p for p in by_type.get('task_update', [])
                if p.get('status') == 'done']
        # Both lanes appeared live and both terminal states arrived — without
        # a wait in between, which is the point of detached workers.
        self.assertEqual({p['handle'] for p in running}, {'t1', 't2'}, by_type)
        self.assertEqual({p['handle'] for p in done}, {'t1', 't2'}, by_type)
        # The lock list was published while the claims were held…
        lease_frames = by_type.get('lease_update', [])
        self.assertTrue(lease_frames, by_type)
        held = [l for f in lease_frames for l in f.get('leases', [])]
        self.assertTrue(any(l['pattern'] == 'src/a.ts' for l in held), held)
        # …and every frame carries what the panel renders, not prose.
        for p in done:
            self.assertTrue(p['label'], p)
            self.assertTrue(p['execution_id'], p)

    def test_a_worker_never_gets_wider_limits_than_its_lead(self):
        """Guardrail C7-7 at dispatch: the lead holds `src/api/**`, the worker
        template holds wider `src/**` — the worker runs with the narrower."""
        from agents.models import SubAgent as _SubAgent

        wide = _SubAgent.objects.create(
            user=self.user, name='Wide', template_slug='code-wide',
            tool_grants={'shell': True, 'subAgents': False},
            guardrails={'autonomy': 'auto', 'spendCapRupees': 600},
            agent_context={'writePaths': ['src/**']},
            allow_unattended=True)
        seen: dict = {}

        async def _capture(*a, **k):
            seen.update(k)
            return FakeRun(answer='ok')

        ctx = _ctx(self.user, write_paths=('src/api/**',))
        task = self.task('t1', agent=wide.id, claims=['src/api/client.ts'])
        with patch('agents.agent.runtime.run_agent', side_effect=_capture):
            out = json.loads(async_to_sync(dispatch.start_tasks)(
                {'tasks': [task]}, ctx))
        self.assertEqual(len(out['started']), 1, out)
        # The template's wider `src/**` narrowed to the lead's scope ∩ claims.
        self.assertEqual(seen.get('write_paths'), ('src/api/client.ts',), seen)

    def test_a_claim_outside_the_lead_scope_writes_nothing(self):
        """The mirror: a claim the lead's own scope does not cover cannot
        widen the worker past it — the effective set is empty, so every write
        is refused rather than silently scoped somewhere else."""
        from agents.models import SubAgent as _SubAgent

        wide = _SubAgent.objects.create(
            user=self.user, name='Wide2', template_slug='code-wide-2',
            tool_grants={'shell': True, 'subAgents': False},
            guardrails={'autonomy': 'auto', 'spendCapRupees': 600},
            agent_context={'writePaths': ['src/**']},
            allow_unattended=True)
        seen: dict = {}

        async def _capture(*a, **k):
            seen.update(k)
            return FakeRun(answer='ok')

        ctx = _ctx(self.user, write_paths=('src/api/**',))
        task = self.task('t1', agent=wide.id, claims=['src/other.ts'])
        with patch('agents.agent.runtime.run_agent', side_effect=_capture):
            out = json.loads(async_to_sync(dispatch.start_tasks)(
                {'tasks': [task]}, ctx))
        self.assertEqual(len(out['started']), 1, out)
        self.assertEqual(seen.get('write_paths'), (), seen)
