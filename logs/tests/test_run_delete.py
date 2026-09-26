"""
Coverage for run deletion: one row, bulk, and mark-as-failed.

Deletion cascades turns/steps/trace but keeps `CostEntry` rows (their
`execution` FK is SET_NULL), so spend totals don't silently drop. Live runs
are refused until stopped, eval-caller runs are refused in favour of deleting
the sweep, and foreign ids are 404 — never 403, so nothing enumerates.
"""
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase

from agents.models import SubAgent
from logs.models import AgentStep, AgentTurn, CostEntry, ExecutionLog


class RunDeleteTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='owner', password='pw')
        self.other = User.objects.create_user(username='stranger', password='pw')
        self.client.force_authenticate(user=self.user)
        self.agent = SubAgent.objects.create(user=self.user, name='Reporter')

    def _run(self, **fields):
        defaults = dict(
            user=self.user, subagent=self.agent, status='completed',
            started_at=timezone.now(),
        )
        defaults.update(fields)
        return ExecutionLog.objects.create(**defaults)

    def _url(self, log):
        return reverse('logs:execution_detail', args=[str(log.execution_id)])

    def test_delete_finished_run_cascades_but_keeps_costs(self):
        log = self._run()
        AgentTurn.objects.create(execution=log, index=1, decision='answer')
        AgentStep.objects.create(
            execution=log, call_id='c1', tool='web_search', status='completed', order=1,
        )
        CostEntry.objects.create(
            user=self.user, execution=log, kind='image', amount_inr=5,
        )
        response = self.client.delete(self._url(log))
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(ExecutionLog.objects.filter(id=log.id).exists())
        self.assertFalse(AgentTurn.objects.filter(execution_id=log.id).exists())
        self.assertFalse(AgentStep.objects.filter(execution_id=log.id).exists())
        kept = CostEntry.objects.filter(user=self.user, kind='image').first()
        self.assertIsNotNone(kept)
        self.assertIsNone(kept.execution_id)

    def test_delete_live_run_is_refused(self):
        for live in ('pending', 'running', 'paused'):
            log = self._run(status=live)
            response = self.client.delete(self._url(log))
            self.assertEqual(response.status_code, status.HTTP_409_CONFLICT, live)
            self.assertTrue(ExecutionLog.objects.filter(id=log.id).exists())

    def test_delete_foreign_or_bad_id_is_404(self):
        mine = self._run()
        self.client.force_authenticate(user=self.other)
        response = self.client.delete(self._url(mine))
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        self.assertTrue(ExecutionLog.objects.filter(id=mine.id).exists())

        self.client.force_authenticate(user=self.user)
        response = self.client.delete(
            reverse('logs:execution_detail', args=['not-a-uuid']))
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_delete_eval_run_points_at_the_sweep(self):
        log = self._run(caller='eval')
        response = self.client.delete(self._url(log))
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.assertTrue(ExecutionLog.objects.filter(id=log.id).exists())

    def test_bulk_delete_ids_is_atomic(self):
        keep = self._run(status='failed')
        live = self._run(status='running')
        url = reverse('logs:execution_bulk_delete')
        response = self.client.post(url, {
            'ids': [str(keep.execution_id), str(live.execution_id)],
        }, format='json')
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        # All or nothing: the failed row survives the refused request.
        self.assertTrue(ExecutionLog.objects.filter(id=keep.id).exists())

        response = self.client.post(url, {
            'ids': [str(keep.execution_id)],
        }, format='json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['deleted'], 1)
        self.assertFalse(ExecutionLog.objects.filter(id=keep.id).exists())

    def test_bulk_delete_failed_clears_only_failed(self):
        self._run(status='failed')
        self._run(status='completed')
        url = reverse('logs:execution_bulk_delete')
        response = self.client.post(url, {'status': 'failed'}, format='json')
        self.assertEqual(response.data['deleted'], 1)
        self.assertEqual(
            ExecutionLog.objects.filter(user=self.user).count(), 1)

    def test_bulk_delete_older_than_uses_completed_at(self):
        old = self._run(status='failed')
        old.completed_at = timezone.now() - timedelta(days=40)
        old.save(update_fields=['completed_at'])
        self._run(status='failed')
        url = reverse('logs:execution_bulk_delete')
        response = self.client.post(
            url, {'status': 'failed', 'older_than_days': 30}, format='json')
        self.assertEqual(response.data['deleted'], 1)
        self.assertFalse(ExecutionLog.objects.filter(id=old.id).exists())

    def test_bulk_delete_needs_ids_or_status(self):
        url = reverse('logs:execution_bulk_delete')
        response = self.client.post(url, {}, format='json')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_mark_failed_closes_a_stuck_run(self):
        log = self._run(status='running')
        url = reverse('logs:execution_mark_failed', args=[str(log.execution_id)])
        response = self.client.post(url, {})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        log.refresh_from_db()
        self.assertEqual(log.status, 'failed')
        self.assertEqual(log.failure_category, 'interrupted')

    def test_mark_failed_refuses_terminal_runs(self):
        log = self._run(status='completed')
        url = reverse('logs:execution_mark_failed', args=[str(log.execution_id)])
        response = self.client.post(url, {})
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)

    def test_mark_failed_foreign_is_404(self):
        log = self._run(status='running')
        self.client.force_authenticate(user=self.other)
        url = reverse('logs:execution_mark_failed', args=[str(log.execution_id)])
        response = self.client.post(url, {})
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
