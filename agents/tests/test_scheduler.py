"""The in-process trigger scheduler: lease, slot claim, detached sweep.

Phase 1 of the schedules plan moved the sweep off the host crontab and into
the backend process. These tests pin the three properties that make that
safe: one holder at a time, one firing per slot, and runs that start without
waiting. `test_triggers.py` / `test_schedules.py` pin the *rules* (they pass
unchanged); these pin the *mechanics*.
"""
from datetime import timedelta
from unittest.mock import patch

from asgiref.sync import async_to_sync
from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone
from rest_framework.reverse import reverse
from rest_framework.test import APITestCase

from agents import scheduler
from agents.models import SchedulerLease, SubAgent, Trigger
from agents.sweep import Launch, prepare


def _holder(name):
    """Run a block as a different scheduler process."""
    return patch.object(scheduler, 'HOLDER', name)


class LeaseTests(TestCase):
    def test_lease_single_holder(self):
        """Two holders. The first `try_acquire` wins and the second fails
        until `expires_at` passes, then the second wins."""
        now = timezone.now()
        with _holder('first'):
            self.assertTrue(scheduler.try_acquire(now))
            # Renewal by the holder always works.
            self.assertTrue(scheduler.try_acquire(now))
        with _holder('second'):
            self.assertFalse(scheduler.try_acquire(now))

        SchedulerLease.objects.filter(name=scheduler.LEASE_NAME).update(
            expires_at=now - timedelta(seconds=1))
        with _holder('second'):
            self.assertTrue(
                scheduler.try_acquire(now + timedelta(seconds=200)))

    def test_scheduler_disabled_in_tests(self):
        """A background loop must never start in the test suite."""
        from django.conf import settings

        self.assertIs(settings.SCHEDULER_ENABLED, False)


class SlotClaimTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='claimer', password='pw')
        self.agent = SubAgent.objects.create(
            user=self.user, name='Nightly', prompt='Check the invoices.',
            allow_unattended=True, llm_provider='nvidia', llm_model='m',
        )
        self.trigger = Trigger.objects.create(
            subagent=self.agent, mode='schedule', config={'cron': '0 9 * * *'},
            goal='Check the invoices.',
            next_due_at=timezone.now() - timedelta(minutes=1),
        )

    def test_slot_claimed_once(self):
        """Two `prepare` calls on the same stale row instance: the first
        returns `Launch` and the second returns `'busy'`, and `next_due_at`
        moved exactly once."""
        now = timezone.now()
        stale_due = self.trigger.next_due_at

        first = prepare(self.trigger, now)
        self.assertIsInstance(first, Launch)
        self.trigger.refresh_from_db()
        moved = self.trigger.next_due_at
        self.assertGreater(moved, stale_due)

        # Same stale instance, as a second sweep that read before the claim.
        self.trigger.next_due_at = stale_due
        second = prepare(self.trigger, now)
        self.assertEqual(second, 'busy')

        self.trigger.refresh_from_db()
        self.assertEqual(self.trigger.next_due_at, moved)


class DetachedSweepTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='loop', password='pw')
        self.agent = SubAgent.objects.create(
            user=self.user, name='Nightly', prompt='Check the invoices.',
            allow_unattended=True, llm_provider='nvidia', llm_model='m',
        )
        self.trigger = Trigger.objects.create(
            subagent=self.agent, mode='schedule', config={'cron': '0 9 * * *'},
            goal='Check the invoices.',
            next_due_at=timezone.now() - timedelta(minutes=1),
        )

    def test_sweep_once_starts_detached(self):
        """`sweep_once` records the start and never waits: the blocking twin
        must not be called on this path."""

        async def fake_start(agent, goal, **kwargs):
            return 'e1'

        def boom(*a, **k):
            raise AssertionError('must not block on the detached path')

        with patch('agents.agent.runtime.start_agent_run', fake_start), \
                patch('agents.agent.runtime.start_agent_run_and_wait', boom):
            counts = async_to_sync(scheduler.sweep_once)()

        self.trigger.refresh_from_db()
        self.assertEqual(counts.get('fired'), 1)
        self.assertEqual(self.trigger.last_outcome, 'fired')
        # 'e1' is a test double's id, not a real run — the link stays empty
        # rather than failing the firing over a convenience.
        self.assertIsNone(self.trigger.last_execution)

    def test_sweep_once_links_a_real_run(self):
        """When the started run exists, the trigger points at it."""
        from logs.models import ExecutionLog

        # Completed, not running: a second in-flight run for this agent
        # would hold the firing `busy` behind the overlap policy, which is
        # the rule working, not the link failing.
        log = ExecutionLog.objects.create(
            user=self.user, subagent=self.agent, status='completed',
        )

        async def fake_start(agent, goal, **kwargs):
            return str(log.execution_id)

        with patch('agents.agent.runtime.start_agent_run', fake_start):
            async_to_sync(scheduler.sweep_once)()

        self.trigger.refresh_from_db()
        self.assertEqual(self.trigger.last_execution_id, log.id)

    def test_refusal_counts_as_failure(self):
        """A guardrail refusal on the detached path counts, like the sync one."""
        from agents.agent.runtime import AgentRunRefused

        async def refuse(*a, **k):
            raise AgentRunRefused('spend cap reached')

        with patch('agents.agent.runtime.start_agent_run', refuse):
            counts = async_to_sync(scheduler.sweep_once)()

        self.trigger.refresh_from_db()
        self.assertEqual(counts.get('refused'), 1)
        self.assertEqual(self.trigger.consecutive_failures, 1)
        self.assertEqual(self.trigger.last_outcome, 'refused')


class RunNowDetachedTests(APITestCase):
    """The button answers when the run *starts*, not when it ends."""

    def setUp(self):
        self.user = User.objects.create_user(username='runner', password='pw')
        self.client.force_authenticate(user=self.user)
        self.agent = SubAgent.objects.create(
            user=self.user, name='Nightly', prompt='Check the invoices.',
            allow_unattended=True, llm_provider='nvidia', llm_model='m',
        )
        self.trigger = Trigger.objects.create(
            subagent=self.agent, mode='schedule', config={'cron': '0 9 * * *'},
            goal='Check the invoices.',
            next_due_at=timezone.now() + timedelta(hours=5),
        )

    def test_run_now_returns_202_without_waiting(self):
        """202 + `execution_id`, and the scheduled slot did not move — a
        manual run is extra, not the next firing."""
        before = self.trigger.next_due_at

        async def fake_start(agent, goal, **kwargs):
            return 'exec-9'

        with patch('agents.agent.runtime.start_agent_run', fake_start):
            response = self.client.post(
                reverse('orchestrator:trigger_run_now', args=[self.trigger.id]),
                {}, format='json',
            )

        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.data['outcome'], 'fired')
        self.assertEqual(response.data['execution_id'], 'exec-9')

        self.trigger.refresh_from_db()
        self.assertEqual(self.trigger.next_due_at, before)


class TriggerStatusTests(APITestCase):
    """One status per trigger, first match wins (the 2.1 table)."""

    def setUp(self):
        self.user = User.objects.create_user(username='status', password='pw')
        self.client.force_authenticate(user=self.user)
        self.agent = SubAgent.objects.create(
            user=self.user, name='Nightly', prompt='Check the invoices.',
            allow_unattended=True, llm_provider='nvidia', llm_model='m',
        )
        self.url = reverse('orchestrator:trigger_list')

    def _lease(self, fresh=True):
        now = timezone.now()
        SchedulerLease.objects.update_or_create(
            name=scheduler.LEASE_NAME,
            defaults={'holder': 'test',
                      'expires_at': now + timedelta(seconds=90),
                      'beat_at': now if fresh else now - timedelta(hours=1)},
        )

    def _make(self, **kwargs):
        params = dict(
            subagent=self.agent, mode='schedule', config={'cron': '0 9 * * *'},
            goal='Check the invoices.',
            next_due_at=timezone.now() + timedelta(hours=5),
        )
        params.update(kwargs)
        return Trigger.objects.create(**params)

    def _statuses(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        rows = response.data if isinstance(response.data, list) else response.data['results']
        return {row['id']: (row['status'], row['status_message']) for row in rows}

    def test_ok_when_nothing_is_wrong(self):
        self._lease()
        trigger = self._make()
        self.assertEqual(self._statuses()[trigger.id], ('ok', ''))

    def test_scheduler_down_when_nobody_is_sweeping(self):
        trigger = self._make()
        status, message = self._statuses()[trigger.id]
        self.assertEqual(status, 'scheduler_down')
        self.assertIn("isn't running", message)

    def test_needs_permission(self):
        self._lease()
        self.agent.allow_unattended = False
        self.agent.save(update_fields=['allow_unattended'])
        trigger = self._make()
        status, message = self._statuses()[trigger.id]
        self.assertEqual(status, 'needs_permission')
        self.assertIn('Nightly', message)

    def test_agent_paused(self):
        self._lease()
        self.agent.status = 'paused'
        self.agent.save(update_fields=['status'])
        trigger = self._make()
        status, _ = self._statuses()[trigger.id]
        self.assertEqual(status, 'agent_paused')

    def test_self_disabled(self):
        self._lease()
        trigger = self._make(enabled=False, consecutive_failures=5,
                             last_error='boom')
        status, message = self._statuses()[trigger.id]
        self.assertEqual(status, 'self_disabled')
        self.assertIn('boom', message)

    def test_ended(self):
        self._lease()
        trigger = self._make(enabled=False, last_outcome='expired')
        self.assertEqual(self._statuses()[trigger.id][0], 'ended')

    def test_paused(self):
        self._lease()
        trigger = self._make(enabled=False)
        status, message = self._statuses()[trigger.id]
        self.assertEqual(status, 'paused')
        self.assertEqual(message, 'Paused.')

    def test_not_started(self):
        self._lease()
        trigger = self._make(
            starts_at=timezone.now() + timedelta(days=30),
            next_due_at=timezone.now() + timedelta(days=30),
        )
        status, message = self._statuses()[trigger.id]
        self.assertEqual(status, 'not_started')
        self.assertTrue(message.startswith('Starts '))

    def test_overdue(self):
        self._lease()
        trigger = self._make(
            next_due_at=timezone.now() - timedelta(minutes=10))
        status, message = self._statuses()[trigger.id]
        self.assertEqual(status, 'overdue')
        self.assertIn('Overdue', message)

    def test_failing(self):
        self._lease()
        trigger = self._make(consecutive_failures=2, last_error='boom')
        status, message = self._statuses()[trigger.id]
        self.assertEqual(status, 'failing')
        self.assertIn('2 in a row', message)
        self.assertIn('boom', message)

    def test_earlier_rule_wins(self):
        """Overdue *and* forbidden: the permission answer comes first."""
        self._lease()
        self.agent.allow_unattended = False
        self.agent.save(update_fields=['allow_unattended'])
        trigger = self._make(
            next_due_at=timezone.now() - timedelta(minutes=10))
        self.assertEqual(self._statuses()[trigger.id][0], 'needs_permission')

    def test_last_run_links_to_the_run(self):
        """`last_run_id` is the execution UUID the Runs page links by."""
        from logs.models import ExecutionLog

        self._lease()
        log = ExecutionLog.objects.create(
            user=self.user, subagent=self.agent, status='completed',
        )
        trigger = self._make(last_execution=log)
        response = self.client.get(self.url)
        row = next(r for r in response.data if r['id'] == trigger.id)
        self.assertEqual(row['last_run_id'], str(log.execution_id))


class SchedulerHealthTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='health', password='pw')
        self.client.force_authenticate(user=self.user)
        self.url = reverse('orchestrator:trigger_health')

    def test_health_endpoint(self):
        """No lease row, stale beat, fresh beat: false/null, false, true."""
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.data['running'])
        self.assertIsNone(response.data['last_tick_at'])

        now = timezone.now()
        SchedulerLease.objects.create(
            name=scheduler.LEASE_NAME, holder='old',
            expires_at=now - timedelta(seconds=1),
            beat_at=now - timedelta(hours=1),
        )
        response = self.client.get(self.url)
        self.assertFalse(response.data['running'])
        self.assertIsNotNone(response.data['last_tick_at'])

        SchedulerLease.objects.filter(name=scheduler.LEASE_NAME).update(
            beat_at=now, expires_at=now + timedelta(seconds=90))
        response = self.client.get(self.url)
        self.assertTrue(response.data['running'])
