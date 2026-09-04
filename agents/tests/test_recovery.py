"""
Runs whose process went away.

A run is a detached task, so when the process holding it dies the task dies
with it and nothing anywhere notices: the `ExecutionLog` stays `running` for
ever, the user watches a stream with no producer, and the agent's stats count a
run that never ended. No exception is raised at any point, which is why this is
a sweep and not a handler.

The orphan test is the interesting part and the reason these tests exist: it
uses only the row and the agent's own declared wall-clock limit, so it keeps
working with any number of worker processes. Anything based on process ids or
boot times would report every run started by a *sibling* worker as orphaned and
kill it mid-flight.
"""
from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch

from asgiref.sync import async_to_sync
from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone

from agents import recovery
from agents.models import SubAgent
from logs.models import ExecutionLog


class OrphanDetectionTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user('orphan', 'o@example.com', 'x')

    def _agent(self, max_run_seconds=600):
        return SubAgent.objects.create(
            user=self.user, name=f'A{max_run_seconds}', prompt='p',
            guardrails={'maxRunSeconds': max_run_seconds},
        )

    def _run(self, agent, age_seconds, status='running'):
        return ExecutionLog.objects.create(
            subagent=agent, user=self.user, status=status,
            started_at=timezone.now() - timedelta(seconds=age_seconds),
            input_data={'thread_id': 'thread-1', 'goal': 'go'},
        )

    def _orphans(self):
        return async_to_sync(recovery._orphans)(20)

    def test_a_run_inside_its_own_limit_is_left_alone(self):
        self._run(self._agent(600), age_seconds=60)
        self.assertEqual(self._orphans(), [])

    def test_a_run_just_past_its_limit_is_still_left_alone(self):
        """The grace covers wrap-up, clock skew and a final checkpoint flush.

        Killing a run that was merely slow throws away work the user has
        already paid for, so the margin errs towards waiting.
        """
        self._run(self._agent(600), age_seconds=600 + 10)
        self.assertEqual(self._orphans(), [])

    def test_a_run_past_its_limit_and_the_grace_is_an_orphan(self):
        log = self._run(self._agent(600),
                        age_seconds=600 + recovery.ORPHAN_GRACE_SECONDS + 60)
        self.assertEqual([entry[0].id for entry in self._orphans()], [log.id])

    def test_the_limit_is_read_per_agent_not_from_a_constant(self):
        """A short-limit agent is orphaned long before a long-limit one.

        A single global threshold would either kill legitimate long runs or
        leave short ones stuck for hours; the run already declared its own
        bound, so that is the one to use.
        """
        short = self._run(self._agent(60), age_seconds=60 + recovery.ORPHAN_GRACE_SECONDS + 30)
        self._run(self._agent(7200), age_seconds=3600)

        self.assertEqual([entry[0].id for entry in self._orphans()], [short.id])

    def test_finished_runs_are_never_candidates(self):
        # One agent, reused: `SubAgent` is unique on (user, name), so building
        # a fresh one per subtest collides on the second pass.
        agent = self._agent(60)
        for state in ('completed', 'failed', 'paused', 'cancelled'):
            with self.subTest(state=state):
                ExecutionLog.objects.all().delete()
                self._run(agent, age_seconds=99999, status=state)
                self.assertEqual(self._orphans(), [])

    def test_a_paused_run_is_never_swept(self):
        """It is waiting for a person, not for a process.

        Sweeping one would fail a run the user is actively being asked about —
        and its checkpoint is deliberately kept for exactly that resume.
        """
        self._run(self._agent(60), age_seconds=99999, status='paused')
        self.assertEqual(self._orphans(), [])


class RecoveryOutcomeTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user('recoverer', 'r@example.com', 'x')

    def setUp(self):
        self.agent = SubAgent.objects.create(
            user=self.user, name='Worker', prompt='p',
            guardrails={'maxRunSeconds': 60},
        )
        self.log = ExecutionLog.objects.create(
            subagent=self.agent, user=self.user, status='running',
            started_at=timezone.now() - timedelta(seconds=9999),
            input_data={'thread_id': 'thread-x', 'goal': 'go'},
        )

    def test_without_a_durable_saver_the_run_is_failed_honestly(self):
        """The state died with the process; saying so beats silently redoing it.

        A resume here would start the work from the beginning on a log that
        already reports partial progress — and charge for it twice.
        """
        with patch('chat.turn.checkpoints.is_durable', return_value=False):
            tally = async_to_sync(recovery.sweep_orphaned_runs)()

        self.log.refresh_from_db()
        self.assertEqual(tally['failed'], 1)
        self.assertEqual(tally['resumed'], 0)
        self.assertEqual(self.log.status, 'failed')
        self.assertIn('interrupted', self.log.error_message)
        self.assertIsNotNone(self.log.completed_at)

    def test_a_durable_saver_with_no_state_still_fails_rather_than_resuming(self):
        """Durability turned on *after* a run started leaves it with no state.

        `is_durable()` answers a question about configuration; whether this
        particular thread has anything saved is a different question, and only
        the second one licenses a resume.
        """
        with patch('chat.turn.checkpoints.is_durable', return_value=True):
            with patch('agents.recovery._has_state', return_value=False) as has:
                async_to_sync(recovery.sweep_orphaned_runs)()
                has.assert_called()

        self.log.refresh_from_db()
        self.assertEqual(self.log.status, 'failed')

    def test_with_durable_state_the_run_is_resumed_on_its_own_id(self):
        """A trace split across two execution ids cannot be joined again."""
        seen = {}

        async def _fake_state(thread_id):
            seen['thread_id'] = thread_id
            return True

        with patch('chat.turn.checkpoints.is_durable', return_value=True):
            with patch('agents.recovery._has_state', _fake_state):
                with patch('workflow_backend.background.spawn') as spawn:
                    tally = async_to_sync(recovery.sweep_orphaned_runs)()
                    spawn.assert_called_once()

        self.assertEqual(tally['resumed'], 1)
        self.assertEqual(seen['thread_id'], 'thread-x')
        self.log.refresh_from_db()
        # Still running: the resumed task owns it now and will close it.
        self.assertEqual(self.log.status, 'running')

    def test_a_run_whose_agent_was_deleted_is_closed_not_retried(self):
        self.log.subagent = None
        self.log.save(update_fields=['subagent'])

        async_to_sync(recovery.sweep_orphaned_runs)()
        self.log.refresh_from_db()
        self.assertEqual(self.log.status, 'failed')
        self.assertIn('no longer exists', self.log.error_message)

    def test_a_run_that_finished_mid_sweep_is_not_overwritten(self):
        """The sweep can run beside the process that owns the run.

        Between selecting the row and writing to it the real owner may have
        finished it, and stamping `failed` over a completed run would report a
        successful run as broken.
        """
        from asgiref.sync import sync_to_async

        async def _finish_first(thread_id):
            # Stands in for the owning process completing the run between the
            # sweep selecting the row and the sweep writing to it.
            await sync_to_async(
                ExecutionLog.objects.filter(id=self.log.id).update
            )(status='completed')
            return False

        with patch('chat.turn.checkpoints.is_durable', return_value=True):
            with patch('agents.recovery._has_state', _finish_first):
                async_to_sync(recovery.sweep_orphaned_runs)()

        self.log.refresh_from_db()
        self.assertEqual(self.log.status, 'completed')


class SweepReachabilityTests(TestCase):
    """Both doors exist, for the reason this sweep in particular needs them."""

    def test_it_is_reachable_as_a_celery_task(self):
        from agents.tasks import recover_runs

        self.assertEqual(recover_runs.name, 'orchestrator.recover_runs')

    def test_it_is_reachable_as_a_management_command(self):
        """Local dev has no broker, and this is the recovery path for a dead
        process — a broker-only design would be missing exactly when needed."""
        from django.core.management import load_command_class

        command = load_command_class('agents', 'recover_runs')
        self.assertTrue(command.help)

    def test_it_is_on_the_beat_schedule(self):
        from django.conf import settings

        tasks = {
            entry['task']
            for entry in settings.CELERY_BEAT_SCHEDULE.values()
        }
        self.assertIn('orchestrator.recover_runs', tasks)
