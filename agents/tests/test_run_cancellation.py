"""
What a cancelled agent run leaves behind.

`asyncio.CancelledError` has been a `BaseException` since 3.8, so `run_agent`'s
`except Exception` never saw it. A cancelled run therefore left its
`ExecutionLog` at `running` for ever: the canvas showed a live spinner with
nothing behind it, the agent's stats counted a run that never ended, and any
`HITLRequest` it had opened stayed `pending` — which means
`notifications/reminders.py` went on escalating it and putting it in the daily
digest, asking the user to approve a step in a run that no longer existed.

Nothing cancels an agent run from the outside yet; the run registry and its
stop endpoint come later. Shutdown, a parent task, and a supervising timeout
all cancel it today, and each one used to leak the same way.
"""
from __future__ import annotations

import asyncio
from unittest.mock import patch

from asgiref.sync import sync_to_async
from django.contrib.auth.models import User
from django.test import TransactionTestCase

from agents.models import HITLRequest, SubAgent


class CancelledRunTests(TransactionTestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='canceller', password='pw')
        self.agent = SubAgent.objects.create(
            user=self.user, name='Slow',             prompt='Take your time.', tool_grants={}, guardrails={},
            llm_provider='nvidia', llm_model='test/model',
        )

    async def _cancel_mid_run(self, on_started=None):
        """Start a run that blocks for ever, cancel it, return its log."""
        from agents.agent.runtime import _open_log, run_agent

        log = await _open_log(self.agent, self.user, 'go', 'manual', 'thread-1')
        if on_started is not None:
            await on_started(log)

        started = asyncio.Event()

        async def blocking_turn(turn, *, prompt, thread_id):
            started.set()
            await asyncio.Event().wait()  # never resolves

        async def no_preflight(**kwargs):
            return None

        with patch('chat.turn.agent.run_turn', blocking_turn), \
                patch('llm.access.preflight', no_preflight):
            task = asyncio.ensure_future(
                run_agent(self.agent, 'go', user=self.user,
                          thread_id='thread-1', log=log)
            )
            await started.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

        await sync_to_async(log.refresh_from_db)()
        return log

    async def test_the_log_is_closed_as_cancelled_not_left_running(self):
        log = await self._cancel_mid_run()

        self.assertEqual(log.status, 'cancelled')
        self.assertIsNotNone(log.completed_at)
        # A duration is what tells a stuck run apart from a cancelled one in
        # every listing that renders them.
        self.assertIsNotNone(log.duration_ms)

    async def test_pending_approvals_are_withdrawn_with_the_run(self):
        """Otherwise the reminder ladder nudges about a step that cannot happen.

        `notifications/reminders.py` sweeps on `status='pending'`, so a request
        left behind escalates at +1h and +1d and joins every daily digest.
        """
        async def open_request(log):
            await HITLRequest.objects.acreate(
                execution=log, user=self.user, request_type='approval',
                title='Permission required', message='Run send_email?',
                status='pending',
            )

        log = await self._cancel_mid_run(on_started=open_request)

        request = await HITLRequest.objects.aget(execution=log)
        self.assertEqual(request.status, 'cancelled')
        self.assertIsNotNone(request.responded_at)

    async def test_cancellation_still_propagates_to_the_caller(self):
        """Finalising must not swallow the cancel.

        A `CancelledError` absorbed here would leave the cancelling task
        believing the run was still going, and `asyncio` treats a task that
        ignores cancellation as still pending at shutdown.
        """
        log = await self._cancel_mid_run()
        self.assertEqual(log.status, 'cancelled')  # reached via `raise`, not a return
