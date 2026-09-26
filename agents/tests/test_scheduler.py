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


class _FakeTask:
    """Stands in for a spawned job: done or not, on demand."""

    def __init__(self):
        self.finished = False

    def done(self):
        return self.finished


class PeriodicJobTests(TestCase):
    """The loop also runs the app's other periodic jobs (2026-09-26).

    Production runs no Celery, so a beat entry nothing else runs simply never
    happens there: run recovery, the recycle-bin purge and checkpoint pruning
    ran nowhere, and the notification sweeps ran from cron as extra Django
    processes inside the backend's memory limit.
    """

    def setUp(self):
        scheduler._last_started.clear()
        scheduler._running.clear()
        self.spawned: list = []

        def fake_spawn(coro, *, name=None):
            coro.close()  # never awaited in these tests
            task = _FakeTask()
            self.spawned.append((name, task))
            return task

        patcher = patch.object(scheduler, 'spawn', side_effect=fake_spawn)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(scheduler._last_started.clear)
        self.addCleanup(scheduler._running.clear)

    def test_every_beat_entry_runs_in_process_or_says_why_not(self):
        from django.conf import settings

        beat = {entry['task']: entry['schedule']
                for entry in settings.CELERY_BEAT_SCHEDULE.values()}
        in_process = {job.task for job in scheduler.PERIODIC_JOBS}
        self.assertEqual(
            set(beat), in_process | set(scheduler.NOT_IN_PROCESS),
            'A periodic job is in CELERY_BEAT_SCHEDULE but not in '
            'agents/scheduler.py (or the reverse). Production runs no Celery, '
            'so a beat entry alone never runs there: add it to PERIODIC_JOBS, '
            'or to NOT_IN_PROCESS with the reason it must not run in-process.')
        self.assertFalse(in_process & set(scheduler.NOT_IN_PROCESS))
        for job in scheduler.PERIODIC_JOBS:
            # Same interval beat would use, read from the same setting.
            self.assertEqual(int(getattr(settings, job.every_setting)), beat[job.task],
                             job.task)

    def test_all_jobs_start_on_the_first_tick(self):
        started = scheduler.start_due_jobs(now_monotonic=1000.0)
        self.assertEqual(started, [job.task for job in scheduler.PERIODIC_JOBS])

    def test_a_job_waits_for_its_interval(self):
        from django.conf import settings

        scheduler.start_due_jobs(now_monotonic=1000.0)
        for _, task in self.spawned:
            task.finished = True
        self.assertEqual(scheduler.start_due_jobs(now_monotonic=1030.0), [])
        # The scheduled-notification sweep is the fastest (a minute); only it
        # is due again after one interval.
        later = 1000.0 + settings.SCHEDULED_SWEEP_SECONDS
        self.assertEqual(scheduler.start_due_jobs(now_monotonic=later),
                         ['notifications.sweep_scheduled'])

    def test_a_job_still_running_is_not_started_twice(self):
        scheduler.start_due_jobs(now_monotonic=1000.0)
        # Nothing finished; hours later, every job is due but still running.
        self.assertEqual(scheduler.start_due_jobs(now_monotonic=1000.0 + 86400), [])

    def test_jobs_run_only_while_this_process_holds_the_lease(self):
        async def one_tick():
            with patch.object(scheduler, 'sweep_once') as sweep,                  patch.object(scheduler, 'start_due_jobs') as jobs,                  patch.object(scheduler, 'try_acquire', return_value=False),                  patch.object(scheduler.asyncio, 'sleep', side_effect=RuntimeError('stop')):
                try:
                    await scheduler.run_forever()
                except RuntimeError:
                    pass
            return sweep.called, jobs.called

        self.assertEqual(async_to_sync(one_tick)(), (False, False))


class PeriodicJobsRunForRealTests(TestCase):
    """Each job, run for real on an empty database: no import typo, no crash."""

    def test_each_job_runs_and_reports(self):
        for job in scheduler.PERIODIC_JOBS:
            with self.subTest(job.task):
                result = async_to_sync(job.run)()
                self.assertIsInstance(result, dict, job.task)
