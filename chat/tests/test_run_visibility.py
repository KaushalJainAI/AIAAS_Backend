"""
The orchestrator's eyes: what this user's jobs are doing.

Pinned: discovery is user-scoped (a foreign execution_id is "no such run",
never a peek), rows are compact (counts and excerpts, never the trace), and
the progress block names what closed-run data versus live activity it came
from — a running run's plan lives in graph state, not on its row, and the
report must not pretend otherwise.
"""
from __future__ import annotations

import json
from datetime import timedelta

from asgiref.sync import async_to_sync
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from agents.models import HITLRequest, SubAgent
from chat.tools import execute_tool
from logs.models import AgentStep, AgentTurn, ExecutionLog

User = get_user_model()


def _run(user, **overrides):
    body = {
        'user': user, 'status': 'running',
        'input_data': {'goal': 'Research EV tariffs'},
        'started_at': timezone.now() - timedelta(minutes=5),
    }
    body.update(overrides)
    return ExecutionLog.objects.create(**body)


class RunVisibilityTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user('owner', 'o@example.com', 'pw')
        self.other = User.objects.create_user('other', 't@example.com', 'pw')
        self.agent = SubAgent.objects.create(
            user=self.user, name='Researcher', prompt='Research.')
        self.ctx = {'user_id': self.user.id}

    def call(self, name, args):
        return json.loads(async_to_sync(execute_tool)(name, args, dict(self.ctx)))

    def test_running_is_the_default_scope(self):
        live = _run(self.user, subagent=self.agent)
        _run(self.user, subagent=self.agent, status='paused')
        _run(self.user, subagent=self.agent, status='completed')
        out = self.call('list_user_runs', {})
        self.assertEqual(out['count'], 2)
        ids = {r['execution_id'] for r in out['runs']}
        self.assertIn(str(live.execution_id), ids)

    def test_status_filter_and_bad_status(self):
        _run(self.user, status='failed', input_data={})
        out = self.call('list_user_runs', {'status': 'failed'})
        self.assertEqual(out['count'], 1)
        self.assertEqual(out['runs'][0]['status'], 'failed')
        out = self.call('list_user_runs', {'status': 'everywhere'})
        self.assertIn('error', out)

    def test_another_users_runs_are_invisible(self):
        _run(self.other, input_data={'goal': 'Theirs'})
        out = self.call('list_user_runs', {'status': 'all'})
        self.assertEqual(out['count'], 0)
        foreign = ExecutionLog.objects.filter(user=self.other).first()
        out = json.loads(async_to_sync(execute_tool)(
            'get_agent_run', {'execution_id': str(foreign.execution_id)},
            dict(self.ctx)))
        self.assertIn('No such run', out.get('error', ''))

    def test_goal_prefers_the_delegation_task(self):
        worker = _run(self.user, subagent=self.agent,
                      delegation_task='Summarise page 4',
                      input_data={'goal': 'Bigger job'})
        out = self.call('list_user_runs', {})
        row = next(r for r in out['runs']
                   if r['execution_id'] == str(worker.execution_id))
        self.assertEqual(row['goal'], 'Summarise page 4')

    def test_progress_counts_turns_steps_and_todos(self):
        log = _run(self.user, subagent=self.agent, output_data={
            'todos': [
                {'text': 'Find sources', 'status': 'done'},
                {'text': 'Draft brief', 'status': 'doing'},
                {'text': 'Send it', 'status': 'open'},
            ],
            'files': [{'name': 'brief.md'}],
        })
        turn = AgentTurn.objects.create(execution=log, index=1,
                                        reasoning='Reading sources.')
        AgentTurn.objects.create(execution=log, index=2,
                                 reasoning='Drafting.')
        AgentStep.objects.create(execution=log, turn=turn, call_id='c1',
                                 tool='web_search', status='completed', order=1)
        out = self.call('list_user_runs', {})
        row = next(r for r in out['runs']
                   if r['execution_id'] == str(log.execution_id))
        self.assertEqual(row['turns'], 2)
        self.assertEqual(row['steps'], 1)
        self.assertEqual(row['todos'], {
            'done': 1, 'total': 3, 'open': ['Draft brief', 'Send it'],
            'source': 'run record'})
        self.assertEqual(row['files'], ['brief.md'])
        self.assertEqual(row['agent'], 'Researcher')

    def test_running_run_without_todos_reports_live_activity(self):
        log = _run(self.user, subagent=self.agent)
        AgentTurn.objects.create(execution=log, index=1,
                                 reasoning='Thinking hard.')
        out = self.call('list_user_runs', {})
        row = next(r for r in out['runs']
                   if r['execution_id'] == str(log.execution_id))
        self.assertNotIn('todos', row)
        self.assertEqual(row['activity']['latest'], 'Thinking hard.')
        self.assertEqual(row['activity']['source'], 'latest turn')

    def test_paused_run_with_pending_request_is_flagged(self):
        log = _run(self.user, subagent=self.agent, status='paused')
        HITLRequest.objects.create(
            execution=log, user=self.user, request_type='approval',
            title='Run this?', message='Please decide.')
        out = self.call('list_user_runs', {})
        row = next(r for r in out['runs']
                   if r['execution_id'] == str(log.execution_id))
        self.assertTrue(row['needs_approval'])

    def test_get_agent_run_carries_the_progress_block(self):
        log = _run(self.user, subagent=self.agent, status='completed',
                   output_data={'answer': 'Done.',
                                'todos': [{'text': 'A', 'status': 'done'}]})
        out = json.loads(async_to_sync(execute_tool)(
            'get_agent_run', {'execution_id': str(log.execution_id)},
            dict(self.ctx)))
        self.assertEqual(out['status'], 'completed')
        progress = out['progress']
        self.assertEqual(progress['goal'], 'Research EV tariffs')
        self.assertEqual(progress['todos']['done'], 1)
        self.assertEqual(progress['agent'], 'Researcher')
        self.assertFalse(progress['needs_approval'])
        self.assertEqual(progress['link'], '/runs')

    def test_limit_is_clamped(self):
        for _ in range(3):
            _run(self.user)
        out = self.call('list_user_runs', {'limit': 2})
        self.assertEqual(out['count'], 2)


class LiveTodosTests(TestCase):
    """Best-effort checkpointer reads: labelled, never failing, never invented."""

    def setUp(self):
        self.user = User.objects.create_user('owner', 'o@example.com', 'pw')

    def test_progress_names_its_source(self):
        from chat.tools.runs import progress_for

        log = _run(self.user)
        live = progress_for(log, live_todos=(1, 3, ['Draft brief']))
        self.assertEqual(live['todos']['source'], 'live')
        record = progress_for(log)
        self.assertNotIn('todos', record)

    def test_a_missing_thread_reads_as_nothing_not_an_error(self):
        from chat.tools.runs import live_todos

        self.assertIsNone(async_to_sync(live_todos)('thread-that-never-ran'))

    def test_live_plan_reaches_the_list_row(self):
        from chat.tools import runs as _runs

        log = _run(self.user, thread_id='t-live-1')
        real = _runs.live_todos

        async def fake(thread_id):
            self.assertEqual(thread_id, 't-live-1')
            return (2, 2, [])

        _runs.live_todos = fake
        try:
            out = json.loads(async_to_sync(execute_tool)(
                'list_user_runs', {}, {'user_id': self.user.id}))
        finally:
            _runs.live_todos = real
        row = next(r for r in out['runs']
                   if r['execution_id'] == str(log.execution_id))
        self.assertEqual(row['todos']['source'], 'live')
        self.assertEqual(row['todos']['done'], 2)
