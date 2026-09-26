"""
Run-detail retention: reasoning and tool payloads age out; the record stays.

Pinned: a finished run older than the retention loses its turn reasoning and
step payloads but keeps its row, status, answer and tool names; a recent run
and a paused run are untouched; the management command dry-runs.
"""
from datetime import timedelta
from io import StringIO

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.utils import timezone

from agents.models import SubAgent
from logs.models import AgentStep, AgentTurn, ExecutionLog
from logs.retention import REDACTED, run_retention_sweep

User = get_user_model()


class RetentionTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user('ret', 'r@example.com', 'x')
        self.agent = SubAgent.objects.create(user=self.user, name='Reporter')

    def run_row(self, days_ago, status='completed'):
        log = ExecutionLog.objects.create(
            user=self.user, subagent=self.agent, status=status,
            started_at=timezone.now() - timedelta(days=days_ago),
            output_data={'answer': 'the report'})
        AgentTurn.objects.create(execution=log, index=1, decision='tools',
                                 reasoning='private thoughts', content='draft')
        AgentStep.objects.create(execution=log, call_id='c1', tool='gmail_read_message',
                                 status='completed', order=1,
                                 args={'args': {'id': 'm1'}}, result={'body': 'private mail'})
        return log

    @override_settings(RUN_DETAIL_RETENTION_DAYS=180)
    def test_old_finished_runs_lose_detail_and_keep_the_record(self):
        old = self.run_row(200)
        recent = self.run_row(10)
        paused = self.run_row(200, status='paused')

        done = run_retention_sweep()
        self.assertEqual((done['turns'], done['steps']), (1, 1))

        turn = AgentTurn.objects.get(execution=old)
        step = AgentStep.objects.get(execution=old)
        self.assertEqual((turn.reasoning, turn.content), ('', ''))
        self.assertEqual(step.result, REDACTED)
        self.assertEqual(step.tool, 'gmail_read_message')
        old.refresh_from_db()
        self.assertEqual(old.output_data['answer'], 'the report')

        for kept in (recent, paused):
            self.assertEqual(AgentTurn.objects.get(execution=kept).reasoning, 'private thoughts')
            self.assertEqual(AgentStep.objects.get(execution=kept).result, {'body': 'private mail'})

        # Idempotent: a second sweep finds nothing new.
        self.assertEqual(run_retention_sweep()['steps'], 0)

    def test_the_command_dry_runs(self):
        self.run_row(400)
        out = StringIO()
        call_command('purge_run_detail', '--dry-run', stdout=out)
        self.assertIn('Would clear 1 turn(s) and 1 step(s)', out.getvalue())
        self.assertEqual(AgentTurn.objects.get().reasoning, 'private thoughts')
