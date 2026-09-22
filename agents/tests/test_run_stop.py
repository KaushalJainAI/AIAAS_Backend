"""
Stopping a run: `POST /api/orchestrator/runs/<execution_id>/cancel/`.

There was no way to stop an agent run. The runtime closed a cancelled task
cleanly — `_finalise_cancelled` — but nothing asked it to, so a run started by
mistake spent until `maxRunSeconds`. Each shape of "in flight" is stopped
differently, and each is pinned here.
"""
from __future__ import annotations

import asyncio
import uuid

from asgiref.sync import async_to_sync, sync_to_async
from django.contrib.auth.models import User
from django.test import TransactionTestCase
from rest_framework.test import APITestCase

from agents.models import HITLRequest, SubAgent
from logs.models import AgentTurn, ExecutionLog


def _url(execution_id) -> str:
    return f'/api/orchestrator/runs/{execution_id}/cancel/'


class StopEndpointTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='stopper', password='pw')
        self.client.force_authenticate(self.user)
        self.agent = SubAgent.objects.create(user=self.user, name='Looper')

    def _run(self, **fields):
        fields.setdefault('status', 'running')
        return ExecutionLog.objects.create(
            user=self.user, subagent=self.agent,
            input_data={'goal': 'g', 'thread_id': f't-{uuid.uuid4()}'}, **fields,
        )

    def test_a_run_paused_for_approval_is_closed_and_its_approval_withdrawn(self):
        """It has no task; the row and the pending approval are all there is."""
        log = self._run(status='paused')
        HITLRequest.objects.create(execution=log, user=self.user,
                                   node_id='call-1', status='pending')

        response = self.client.post(_url(log.execution_id))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['status'], 'cancelled')
        log.refresh_from_db()
        self.assertEqual(log.status, 'cancelled')
        self.assertFalse(
            HITLRequest.objects.filter(execution=log, status='pending').exists())

    def test_a_delegated_worker_names_its_parent_instead(self):
        from logs.models import AgentStep

        parent = self._run()
        turn = AgentTurn.objects.create(execution=parent, index=1)
        step = AgentStep.objects.create(execution=parent, turn=turn,
                                        tool='invoke_subagent')
        child = self._run(parent_step=step)

        response = self.client.post(_url(child.execution_id))

        self.assertEqual(response.status_code, 409)
        self.assertIn('delegated', response.data['error'])

    def test_a_run_going_elsewhere_is_not_marked_cancelled(self):
        """No task here means another process or an orphan — not "stopped"."""
        log = self._run()
        response = self.client.post(_url(log.execution_id))
        self.assertEqual(response.status_code, 409)
        log.refresh_from_db()
        self.assertEqual(log.status, 'running')

    def test_a_finished_run_reports_how_it_ended(self):
        log = self._run(status='completed')
        response = self.client.post(_url(log.execution_id))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['status'], 'completed')

    def test_someone_elses_run_is_not_found(self):
        other = User.objects.create_user(username='other', password='pw')
        log = ExecutionLog.objects.create(user=other, status='paused')
        self.assertEqual(self.client.post(_url(log.execution_id)).status_code, 404)

    def test_a_malformed_id_is_a_404_not_a_500(self):
        self.assertEqual(self.client.post(_url('not-a-uuid')).status_code, 404)


class WorkerEndpointTests(APITestCase):
    """The plan panel's per-lane buttons, addressed by execution.

    `agent_steer` addresses the latest running run of an agent — the wrong
    worker when one implementer has two going — so these address the execution
    the lane already shows.
    """

    def setUp(self):
        from chat.turn import steering

        steering.clear()
        self.addCleanup(steering.clear)
        self.user = User.objects.create_user(username='panel', password='pw')
        self.client.force_authenticate(self.user)
        self.agent = SubAgent.objects.create(user=self.user, name='Impl')

    def _run(self, **fields):
        fields.setdefault('status', 'running')
        return ExecutionLog.objects.create(
            user=self.user, subagent=self.agent,
            input_data={'goal': 'g', 'thread_id': f't-{uuid.uuid4()}'}, **fields,
        )

    def test_steer_lands_in_the_workers_mailbox(self):
        from chat.turn import steering

        log = self._run()
        thread = log.input_data['thread_id']
        response = self.client.post(
            f'/api/orchestrator/runs/{log.execution_id}/steer/',
            {'message': 'also check the changelog'}, format='json')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(steering.take(thread), 'also check the changelog')

    def test_autonomy_switches_the_worker_mid_run(self):
        from chat.turn import steering

        log = self._run()
        thread = log.input_data['thread_id']
        response = self.client.post(
            f'/api/orchestrator/runs/{log.execution_id}/autonomy/',
            {'level': 'auto'}, format='json')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(steering.autonomy(thread), 'auto')

    def test_a_finished_worker_is_409_not_silently_accepted(self):
        log = self._run(status='completed')
        response = self.client.post(
            f'/api/orchestrator/runs/{log.execution_id}/steer/',
            {'message': 'hi'}, format='json')
        self.assertEqual(response.status_code, 409)

    def test_someone_elses_worker_is_404(self):
        other = User.objects.create_user(username='other', password='pw')
        log = ExecutionLog.objects.create(user=other, status='running')
        response = self.client.post(
            f'/api/orchestrator/runs/{log.execution_id}/steer/',
            {'message': 'hi'}, format='json')
        self.assertEqual(response.status_code, 404)


class LiveTaskTests(TransactionTestCase):
    """A task in this process is cancelled, and its own handler closes the run."""

    def test_the_task_is_found_by_name_and_cancelled(self):
        from agents.agent.runtime import _close_log, cancel_agent_run

        user = User.objects.create_user(username='live', password='pw')
        log = ExecutionLog.objects.create(user=user, status='running')

        async def scenario():
            async def fake_run():
                try:
                    await asyncio.sleep(60)
                except asyncio.CancelledError:
                    # What `_finalise_cancelled` does on the real path.
                    await _close_log(log, status='cancelled', result={},
                                     tokens=0, error='Run cancelled.')
                    raise

            task = asyncio.get_running_loop().create_task(
                fake_run(), name=f'agent-run:{log.execution_id}')
            await asyncio.sleep(0)
            fresh = await sync_to_async(ExecutionLog.objects.get)(id=log.id)
            final = await cancel_agent_run(fresh)
            return final, task.cancelled()

        final, cancelled = async_to_sync(scenario)()
        self.assertEqual(final, 'cancelled')
        self.assertTrue(cancelled)


class ClosedRunsKeepTheirTokensTests(TransactionTestCase):
    """Cancel, timeout and failure close with `tokens=0`; the turns still ran."""

    def test_tokens_come_from_the_turns_that_ran(self):
        from agents.agent.runtime import _close_log

        user = User.objects.create_user(username='tok', password='pw')
        log = ExecutionLog.objects.create(user=user, status='running')
        AgentTurn.objects.create(execution=log, index=1, tokens=700)
        AgentTurn.objects.create(execution=log, index=2, tokens=300)

        async_to_sync(_close_log)(log, status='cancelled', result={}, tokens=0)

        log.refresh_from_db()
        self.assertEqual(log.tokens_used, 1000)
