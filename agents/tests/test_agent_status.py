"""
`paused` is a statement about automation, and it has to be enforced.

The builder has said "Paused: schedules stop firing and no agent may delegate to
it" since `status` became writable, and until this module nothing checked it:
the sweep and the delegation path both ran a paused agent exactly like an active
one. Two layers now hold it — the sweep skips a paused agent's slot without
counting a failure, and the runtime refuses every unattended caller — while the
owner can still run it by hand.
"""
from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch

from asgiref.sync import async_to_sync
from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone

from agents.models import SubAgent, Trigger


class PausedAgentSweepTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='pauser', password='pw')
        self.agent = SubAgent.objects.create(
            user=self.user, name='Nightly', prompt='Check the invoices.',
            allow_unattended=True, llm_provider='nvidia', llm_model='m',
            status='paused',
        )
        self.trigger = Trigger.objects.create(
            subagent=self.agent, mode='schedule', config={'cron': '0 9 * * *'},
            goal='Check the invoices.',
            next_due_at=timezone.now() - timedelta(minutes=1),
        )

    def _sweep(self):
        from agents import sweep

        started = []

        async def fake_start(agent, goal, **kwargs):
            started.append(agent.id)
            return 'exec-1'

        with patch('agents.agent.runtime.start_agent_run', fake_start):
            counts = sweep.run_trigger_sweep()
        return counts, started

    def test_a_paused_agent_does_not_fire(self):
        counts, started = self._sweep()

        self.trigger.refresh_from_db()
        self.assertEqual(started, [])
        self.assertEqual(counts.get('paused'), 1)
        self.assertEqual(self.trigger.last_outcome, 'paused')
        self.assertGreater(self.trigger.next_due_at, timezone.now())

    def test_a_long_pause_does_not_disable_the_schedule(self):
        """A refusal would count toward the failure limit; a skip must not.

        Otherwise pausing an agent for a week switches its schedules off for
        good, and setting it back to active would not bring them back.
        """
        from agents import sweep

        for _ in range(sweep.MAX_CONSECUTIVE_FAILURES + 2):
            Trigger.objects.filter(id=self.trigger.id).update(
                next_due_at=timezone.now() - timedelta(minutes=1)
            )
            self._sweep()

        self.trigger.refresh_from_db()
        self.assertTrue(self.trigger.enabled)
        self.assertEqual(self.trigger.consecutive_failures, 0)

    def test_setting_it_back_to_active_resumes_firing(self):
        self._sweep()
        self.agent.status = 'active'
        self.agent.save(update_fields=['status'])
        Trigger.objects.filter(id=self.trigger.id).update(
            next_due_at=timezone.now() - timedelta(minutes=1)
        )

        counts, started = self._sweep()

        self.assertEqual(counts.get('fired'), 1)
        self.assertEqual(started, [self.agent.id])

    def test_a_firing_owed_from_before_the_pause_is_dropped(self):
        Trigger.objects.filter(id=self.trigger.id).update(
            queued_for=timezone.now() - timedelta(minutes=5),
        )
        self._sweep()

        self.trigger.refresh_from_db()
        self.assertIsNone(self.trigger.queued_for)


class PausedAgentRuntimeTests(TestCase):
    """The runtime is the second wall: delegation never passes the sweep."""

    def setUp(self):
        self.user = User.objects.create_user(username='owner', password='pw')
        self.agent = SubAgent.objects.create(
            user=self.user, name='Worker', prompt='Do the thing.',
            allow_unattended=True, status='paused',
        )

    def test_unattended_callers_are_refused(self):
        from agents.agent.runtime import AgentPaused, _check_status

        for caller in ('trigger', 'orchestrator'):
            with self.subTest(caller=caller):
                with self.assertRaises(AgentPaused):
                    _check_status(self.agent, caller)

    def test_the_owner_can_still_run_it(self):
        """Pausing is about automation; refusing its owner would be deleting."""
        from agents.agent.runtime import _check_status

        for caller in ('api', 'chat'):
            with self.subTest(caller=caller):
                _check_status(self.agent, caller)

    def test_active_and_draft_agents_are_not_affected(self):
        from agents.agent.runtime import _check_status

        for status in ('active', 'draft'):
            self.agent.status = status
            with self.subTest(status=status):
                _check_status(self.agent, 'orchestrator')

    def test_start_agent_run_refuses_before_anything_is_opened(self):
        """Refused before the log exists, so a paused agent leaves no dead run."""
        from agents.agent.runtime import AgentPaused, start_agent_run
        from logs.models import ExecutionLog

        with self.assertRaises(AgentPaused):
            async_to_sync(start_agent_run)(
                self.agent, 'go', user=self.user, caller='orchestrator',
            )
        self.assertFalse(ExecutionLog.objects.filter(subagent=self.agent).exists())

    def test_the_refusal_tells_the_caller_how_to_fix_it(self):
        from agents.agent.runtime import AgentPaused, _check_status

        with self.assertRaises(AgentPaused) as ctx:
            _check_status(self.agent, 'trigger')
        self.assertIn('paused', str(ctx.exception))
        self.assertIn('active', str(ctx.exception))
