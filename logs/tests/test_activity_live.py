"""
Coverage for `GET /api/activity/live/` and `/api/activity/recent-files/`.

The live feed unions five sources with different storage: database rows
(runs, sweeps, worlds) that are visible everywhere, and per-process memory
(code tasks, chat turns) that this process sees. Tests pin the union shape,
the owner filter, and the per-kind action matrix — notably that a generating
world is open-only, because no cancel route exists for one.
"""
from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase

from agents.agent import tasks as code_tasks
from agents.models import SubAgent
from chat.models import ChatSession
from chat.turn import runs as chat_runs
from eval.models import EvalRun, EvalSuite, EvalWorld
from logs.models import ExecutionLog


class ActivityLiveTests(APITestCase):
    url = None

    def setUp(self):
        self.user = User.objects.create_user(username='owner', password='pw')
        self.other = User.objects.create_user(username='stranger', password='pw')
        self.client.force_authenticate(user=self.user)
        self.url = reverse('activity:activity_live')
        self.agent = SubAgent.objects.create(user=self.user, name='Reporter')

    def tearDown(self):
        code_tasks.clear()
        for key in [k for k, r in chat_runs._runs.items() if r.user_id == self.user.id]:
            chat_runs._runs.pop(key, None)

    def _live(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        return {i['kind']: i for i in response.data['items']}

    def test_agent_run_appears_with_stop_and_open(self):
        ExecutionLog.objects.create(
            user=self.user, subagent=self.agent, status='running',
            trigger_type='manual', caller='api', tokens_used=1000,
            started_at=timezone.now(),
        )
        by_kind = self._live()
        row = by_kind['agent_run']
        self.assertEqual(row['title'], 'Reporter')
        self.assertEqual(row['status'], 'running')
        self.assertIn('stop', row['actions'])
        self.assertIn('open', row['actions'])
        self.assertTrue(row['href'].startswith('/runs?run='))
        self.assertGreaterEqual(row['spend_rupees'], 0)

    def test_finished_and_eval_runs_are_not_live(self):
        ExecutionLog.objects.create(
            user=self.user, subagent=self.agent, status='completed',
        )
        ExecutionLog.objects.create(
            user=self.user, subagent=self.agent, status='running', caller='eval',
        )
        by_kind = self._live()
        self.assertNotIn('agent_run', by_kind)

    def test_other_users_rows_never_appear(self):
        ExecutionLog.objects.create(
            user=self.other, subagent=SubAgent.objects.create(
                user=self.other, name='Theirs'),
            status='running',
        )
        self.assertNotIn('agent_run', self._live())

    def test_code_task_resolves_owner_through_its_run(self):
        log = ExecutionLog.objects.create(
            user=self.user, subagent=self.agent, status='running',
        )
        record = code_tasks.CodeTask(
            handle='h1', task_id='t1', title='Add retry',
            agent_id=self.agent.id, agent_name='Implementer', label='Implementer',
            project_id=None, project_name='api', execution_id=str(log.execution_id),
        )
        code_tasks._tasks['lead-thread'] = {'h1': record}
        row = self._live()['code_task']
        self.assertEqual(row['title'], 'Add retry')
        self.assertEqual(row['actions'], ['stop', 'steer', 'open'])
        self.assertEqual(row['project'], 'api')

    def test_code_task_without_a_run_is_skipped(self):
        record = code_tasks.CodeTask(
            handle='h1', task_id='t1', title='Orphan',
            agent_id=None, agent_name='X', label='X',
            project_id=None, project_name='P', execution_id='',
        )
        code_tasks._tasks['lead'] = {'h1': record}
        self.assertNotIn('code_task', self._live())

    def test_eval_sweep_can_be_stopped_and_world_is_open_only(self):
        suite = EvalSuite.objects.create(user=self.user, name='Helpfulness')
        EvalRun.objects.create(
            suite=suite, subagent=self.agent, user=self.user, status='running',
        )
        EvalWorld.objects.create(suite=suite, version=1, status='generating')
        by_kind = self._live()
        self.assertEqual(by_kind['eval_sweep']['actions'], ['stop', 'open'])
        self.assertIn('Helpfulness', by_kind['eval_sweep']['title'])
        # No cancel route exists for a world — open only, by design.
        self.assertEqual(by_kind['eval_world']['actions'], ['open'])

    def test_chat_turn_lists_my_session_only(self):
        mine = ChatSession.objects.create(user=self.user, title='Refund')
        theirs = ChatSession.objects.create(user=self.other, title='Theirs')
        chat_runs._runs[str(mine.id)] = chat_runs.ChatRun(
            key=str(mine.id), user_id=self.user.id)
        chat_runs._runs[str(theirs.id)] = chat_runs.ChatRun(
            key=str(theirs.id), user_id=self.other.id)
        try:
            by_kind = self._live()
            row = by_kind['chat_turn']
            self.assertEqual(row['id'], str(mine.id))
            self.assertEqual(row['title'], 'Refund')
            self.assertEqual(row['actions'], ['stop', 'open'])
        finally:
            chat_runs._runs.pop(str(mine.id), None)
            chat_runs._runs.pop(str(theirs.id), None)


class ActivityRecentFilesTests(APITestCase):
    def test_versions_and_code_changes_appear_newest_first(self):
        from workspaces.models import CodeChange

        user = User.objects.create_user(username='filer', password='pw')
        self.client.force_authenticate(user=user)
        agent = SubAgent.objects.create(user=user, name='Writer')
        log = ExecutionLog.objects.create(user=user, subagent=agent, status='completed')
        CodeChange.objects.create(run=log, path='src/api.py')
        response = self.client.get(reverse('activity:activity_recent_files'))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        kinds = [i['kind'] for i in response.data['items']]
        self.assertIn('code_change', kinds)
        change = next(i for i in response.data['items'] if i['kind'] == 'code_change')
        self.assertEqual(change['path'], 'src/api.py')
        self.assertTrue(change['href'].startswith('/runs?run='))
