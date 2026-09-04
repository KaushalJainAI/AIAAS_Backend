"""
Delegation provenance: who asked for this run, and what were they thinking?

A worker run used to be indistinguishable from any other: same `trigger_type`,
no link to the run that started it, and no record of the instruction it was
given. `ExecutionLog.parent_step` fixes all three at once by pointing at the
*tool call* that delegated — so the orchestrating run is `parent_step.execution`
and its reasoning is `parent_step.turn.reasoning`.
"""
from unittest.mock import AsyncMock

from asgiref.sync import async_to_sync
from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase

from agents.agent.runtime import _open_log
from agents.agent.stream import AgentRunStream
from agents.models import SubAgent
from chat.turn.events import Event
from logs.models import AgentStep, AgentTurn, ExecutionLog


class DelegationLinkTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='boss', password='pw')
        self.client.force_authenticate(user=self.user)

        self.orchestrator = SubAgent.objects.create(
            user=self.user, name='Orchestrator',
            tool_grants={'subAgents': True},
        )
        self.worker_agent = SubAgent.objects.create(
            user=self.user, name='Worker', tool_grants={'webSearch': True},
        )

        # A parent run that reasons, then delegates.
        self.parent = ExecutionLog.objects.create(
            user=self.user, subagent=self.orchestrator, status='running',
            caller='api', input_data={'goal': 'Research X', 'thread_id': 'p-1'},
            started_at=timezone.now(),
        )
        stream = AgentRunStream(self.parent, broadcaster=AsyncMock())
        async_to_sync(stream.on_model_turn)(
            index=1,
            reasoning='This splits into three independent questions, '
                      'so I will fan out rather than do it serially.',
            content='', decision='tools', provider='openrouter',
            model_id='m', tokens=200, duration_ms=80,
        )
        async_to_sync(stream.sink)(Event.AGENT_TRACE, {
            'sub_type': 'tool', 'tool': 'invoke_subagent',
            'args': {'tasks': ['a', 'b']}, 'call_id': 'call-fanout',
        })
        self.step = AgentStep.objects.get(
            execution=self.parent, call_id='call-fanout'
        )

    def _worker(self, index, task):
        return async_to_sync(_open_log)(
            self.worker_agent, self.user, task, 'api', f'sub-{index}',
            caller='orchestrator', depth=1, parent_step_id=self.step.id,
            delegation_task=task, delegation_index=index,
        )

    def test_a_worker_run_names_the_step_that_asked_for_it(self):
        worker = self._worker(0, 'Answer question a')

        self.assertEqual(worker.parent_step_id, self.step.id)
        self.assertEqual(worker.caller, 'orchestrator')
        self.assertEqual(worker.depth, 1)
        self.assertEqual(worker.delegation_task, 'Answer question a')
        self.assertTrue(worker.is_delegated)

    def test_the_orchestrators_reasoning_is_one_hop_from_the_worker(self):
        """The point of pointing at a step rather than at a run."""
        worker = self._worker(0, 'Answer question a')
        worker.refresh_from_db()

        self.assertIn('fan out', worker.parent_step.turn.reasoning)
        self.assertEqual(worker.parent_step.execution_id, self.parent.id)

    def test_the_parent_step_lists_every_run_it_spawned_in_order(self):
        self._worker(1, 'Answer question b')
        self._worker(0, 'Answer question a')

        children = self.step.delegated_runs.order_by('delegation_index')
        self.assertEqual(
            [c.delegation_task for c in children],
            ['Answer question a', 'Answer question b'],
        )

    def test_a_worker_survives_its_parents_steps_being_deleted(self):
        """SET_NULL: a worker run stays interesting on its own."""
        worker = self._worker(0, 'Answer question a')
        self.step.delete()

        worker.refresh_from_db()
        self.assertIsNone(worker.parent_step_id)
        self.assertEqual(worker.delegation_task, 'Answer question a')

    def test_an_undelegated_run_carries_no_provenance(self):
        direct = async_to_sync(_open_log)(
            self.orchestrator, self.user, 'go', 'manual', 't-x', caller='api',
        )
        self.assertIsNone(direct.parent_step_id)
        self.assertFalse(direct.is_delegated)
        self.assertEqual(direct.depth, 0)


class DelegationEndpointTests(APITestCase):
    """What the run-detail endpoint exposes about a delegation, both ways."""

    def setUp(self):
        self.user = User.objects.create_user(username='viewer', password='pw')
        self.client.force_authenticate(user=self.user)
        self.orchestrator = SubAgent.objects.create(user=self.user, name='Boss')
        self.worker_agent = SubAgent.objects.create(user=self.user, name='Hand')

        self.parent = ExecutionLog.objects.create(
            user=self.user, subagent=self.orchestrator, status='completed',
            input_data={'goal': 'Research X'}, started_at=timezone.now(),
        )
        self.turn = AgentTurn.objects.create(
            execution=self.parent, index=1, decision='tools',
            reasoning='Splitting the work three ways.',
        )
        self.step = AgentStep.objects.create(
            execution=self.parent, turn=self.turn, call_id='call-1',
            tool='invoke_subagent', status='completed', order=1,
        )
        self.worker = ExecutionLog.objects.create(
            user=self.user, subagent=self.worker_agent, status='completed',
            caller='orchestrator', depth=1, parent_step=self.step,
            delegation_task='Answer question a', delegation_index=0,
            tokens_used=90, started_at=timezone.now(),
        )

    def _detail(self, execution):
        return self.client.get(
            reverse('logs:execution_detail', args=[str(execution.execution_id)])
        )

    def test_the_worker_run_reports_who_delegated_and_why(self):
        response = self._detail(self.worker)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        by = response.data['delegated_by']
        self.assertEqual(by['workflow_name'], 'Boss')
        self.assertEqual(by['tool'], 'invoke_subagent')
        self.assertEqual(by['task'], 'Answer question a')
        self.assertEqual(by['reasoning'], 'Splitting the work three ways.')
        self.assertEqual(by['turn_index'], 1)

    def test_the_orchestrator_run_lists_the_workers_under_the_step(self):
        response = self._detail(self.parent)
        step = response.data['turns'][0]['steps'][0]

        self.assertEqual(len(step['delegated_runs']), 1)
        child = step['delegated_runs'][0]
        self.assertEqual(child['workflow_name'], 'Hand')
        self.assertEqual(child['task'], 'Answer question a')
        self.assertEqual(child['tokens_used'], 90)

    def test_an_ordinary_run_reports_no_delegation(self):
        plain = ExecutionLog.objects.create(
            user=self.user, subagent=self.worker_agent, status='completed',
            started_at=timezone.now(),
        )
        self.assertIsNone(self._detail(plain).data['delegated_by'])

    def test_runs_can_be_filtered_by_who_started_them(self):
        url = reverse('logs:execution_list')
        response = self.client.get(url, {'caller': 'orchestrator'})
        self.assertEqual(response.data['count'], 1)
        self.assertEqual(
            response.data['results'][0]['execution_id'],
            str(self.worker.execution_id),
        )
        self.assertTrue(response.data['results'][0]['is_delegated'])

    def test_an_unknown_caller_is_rejected_rather_than_returning_nothing(self):
        """An empty list would read as 'no such runs' instead of 'no such filter'."""
        url = reverse('logs:execution_list')
        response = self.client.get(url, {'caller': 'nonsense'})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_statistics_break_runs_down_by_caller(self):
        response = self.client.get(reverse('logs:execution_statistics'))
        self.assertEqual(response.data['by_caller']['orchestrator'], 1)
