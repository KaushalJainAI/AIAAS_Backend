"""Sweeps survive a restart: stale `running` runs are closed, live ones left alone."""
from datetime import timedelta

from asgiref.sync import async_to_sync
from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone

from agents.models import SubAgent
from eval import recovery
from eval.models import EvalResult, EvalRun, EvalSuite
from workflow_backend.thresholds import EVAL_ORPHAN_SECONDS


class EvalRecoveryTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user('rec', 'rec@example.com', 'pw')
        self.agent = SubAgent.objects.create(user=self.user, name='A')
        self.suite = EvalSuite.objects.create(
            user=self.user, name='S', subagent=self.agent)

    def _run(self, **kwargs):
        defaults = dict(suite=self.suite, subagent=self.agent, user=self.user,
                        status='running', supervision='none')
        run = EvalRun.objects.create(**{**defaults, **kwargs})
        return run

    def test_stale_run_is_closed_and_results_errored(self):
        run = self._run()
        EvalResult.objects.create(run=run, status='running')
        EvalResult.objects.create(run=run, status='pending')
        old = timezone.now() - timedelta(seconds=EVAL_ORPHAN_SECONDS + 60)
        EvalRun.objects.filter(pk=run.pk).update(updated_at=old)

        tally = async_to_sync(recovery.sweep_orphaned_eval_runs)()

        self.assertEqual(tally['closed'], 1)
        run.refresh_from_db()
        self.assertEqual(run.status, 'failed')
        self.assertIn('interrupted', run.error_message)
        self.assertTrue(
            EvalResult.objects.filter(run=run, status='error').count() >= 2)

    def test_recent_run_is_left_alone(self):
        run = self._run()
        tally = async_to_sync(recovery.sweep_orphaned_eval_runs)()
        self.assertEqual(tally['checked'], 0)
        run.refresh_from_db()
        self.assertEqual(run.status, 'running')

    def test_finished_between_read_and_write_is_not_overwritten(self):
        run = self._run()
        old = timezone.now() - timedelta(seconds=EVAL_ORPHAN_SECONDS + 60)
        EvalRun.objects.filter(pk=run.pk).update(updated_at=old)
        # Finish it after the read list was built but before the write.
        EvalRun.objects.filter(pk=run.pk).update(status='completed')
        tally = async_to_sync(recovery.sweep_orphaned_eval_runs)()
        self.assertEqual(tally['closed'], 0)
        run.refresh_from_db()
        self.assertEqual(run.status, 'completed')
