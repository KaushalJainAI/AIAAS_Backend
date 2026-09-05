"""
The builder's "Notify me when it stops to ask", end to end.

Grouped by the mistake each test exists to catch. They descend from two, and
the second is the reason the first went unnoticed for so long:

  * **Nothing created a `HITLRequest`.** The reminder engine, the Inbox queue
    and the digest were all written and tested against rows the tests made
    themselves; no production path had ever written one since the DAG-era
    supervisor was retired. So an agent that paused produced a socket frame and
    an ad-hoc `Notification`, the escalation ladder never armed, and
    `agents/hitl/pending/` always answered with an empty list.
  * **The switch read nothing.** `guardrails['notifyOnHitl']` was serialized,
    stored and round-tripped, and no code anywhere consulted it — the third
    dead switch of its kind, after `allowUnattended` and `connectors`.

The property that ties the two together, and the one most of these tests are
really about: **the row is the queue, the notification is the ping.** Turning
notifications off must never remove the pause from the Inbox, because then the
run could not be answered at all.
"""
from unittest.mock import patch

from asgiref.sync import async_to_sync
from django.contrib.auth.models import User
from django.test import TestCase

from agents.models import HITLRequest, SubAgent
from logs.models import ExecutionLog
from notifications.models import HITLReminderSchedule, Notification
from notifications.reminders import pending_for_nudges


class AgentHITLTestCase(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='owner', password='pw')

    def agent(self, **guardrails):
        return SubAgent.objects.create(
            user=self.user, name='Agent', guardrails=guardrails,
        )

    def execution(self, agent, thread_id='thread-1'):
        # Both copies, as `_open_log` writes them: closing a request filters
        # on the indexed column, not the JSON path.
        return ExecutionLog.objects.create(
            user=self.user, subagent=agent, status='paused',
            thread_id=thread_id,
            input_data={'thread_id': thread_id, 'goal': 'do the thing'},
        )

    def open(self, log, call_id='call-1', tool='send_email'):
        """Open a queue entry the way `stream._approval_requested` does."""
        from agents.agent.hitl import open_request

        # The ladder arms in `transaction.on_commit`, which never fires under
        # TestCase's wrapping transaction unless it is captured.
        with self.captureOnCommitCallbacks(execute=True):
            async_to_sync(open_request)(
                log, call_id=call_id, tool=tool,
                message=f'The agent wants to call {tool}.',
            )
        return HITLRequest.objects.filter(execution=log, node_id=call_id).first()


class QueueWriteTests(AgentHITLTestCase):
    """The missing write, without which none of the rest can happen."""

    @patch('notifications.reminders.push_device_notification')
    def test_a_paused_call_becomes_a_pending_request(self, _push):
        log = self.execution(self.agent())
        request = self.open(log)

        self.assertIsNotNone(request)
        self.assertEqual(request.status, 'pending')
        self.assertEqual(request.user_id, self.user.id)
        self.assertEqual(request.request_type, 'approval')

    @patch('notifications.reminders.push_device_notification')
    def test_it_carries_what_answering_it_needs(self, _push):
        """A queue entry that cannot be acted on is just a log line.

        `agents/{id}/approve/` is keyed on the thread and the call, neither of
        which is derivable from the request row itself.
        """
        agent = self.agent()
        log = self.execution(agent, thread_id='thread-xyz')
        request = self.open(log, call_id='call-7')

        self.assertEqual(request.context_data['thread_id'], 'thread-xyz')
        self.assertEqual(request.context_data['call_id'], 'call-7')
        self.assertEqual(request.context_data['agent_id'], agent.id)

    @patch('notifications.reminders.push_device_notification')
    def test_reopening_the_same_call_does_not_queue_it_twice(self, _push):
        """`interrupt()` re-runs the node from the top, so this path repeats.

        Two rows would mean two escalation ladders nudging about one question.
        """
        log = self.execution(self.agent())
        self.open(log, call_id='call-1')
        self.open(log, call_id='call-1')

        self.assertEqual(HITLRequest.objects.filter(execution=log).count(), 1)

    @patch('notifications.reminders.push_device_notification')
    def test_a_queue_failure_does_not_propagate(self, _push):
        """A run that has already stopped and asked must not then crash.

        The live socket prompt is still there; losing the row costs the Inbox
        entry, not the approval.
        """
        log = self.execution(self.agent())
        with patch('agents.models.HITLRequest.objects.aget_or_create',
                   side_effect=RuntimeError('db down')):
            from agents.agent.hitl import open_request

            async_to_sync(open_request)(
                log, call_id='c', tool='t', message='m',
            )  # must not raise


class NotifyToggleTests(AgentHITLTestCase):
    """What `notifyOnHitl` actually switches — and what it must not."""

    @patch('notifications.reminders.push_device_notification')
    def test_on_by_default_arms_the_ladder(self, _push):
        log = self.execution(self.agent())
        request = self.open(log)

        self.assertTrue(
            HITLReminderSchedule.objects.filter(hitl_request=request).exists()
        )

    @patch('notifications.reminders.push_device_notification')
    def test_an_agent_predating_the_setting_still_notifies(self, _push):
        """Absent must read as on. Silence is a choice a user has to make."""
        log = self.execution(self.agent())  # guardrails == {}
        request = self.open(log)

        self.assertTrue(
            HITLReminderSchedule.objects.filter(hitl_request=request).exists()
        )

    @patch('notifications.reminders.push_device_notification')
    def test_off_does_not_arm_the_ladder(self, push):
        log = self.execution(self.agent(notifyOnHitl=False))
        request = self.open(log)

        self.assertFalse(
            HITLReminderSchedule.objects.filter(hitl_request=request).exists()
        )
        push.assert_not_called()

    @patch('notifications.reminders.push_device_notification')
    def test_off_still_queues_the_request(self, _push):
        """The whole point. Off means "don't ping me", never "drop the run".

        A suppressed row would leave the agent paused with nothing in the Inbox
        to answer, so the toggle would quietly mean "abandon this run".
        """
        log = self.execution(self.agent(notifyOnHitl=False))
        request = self.open(log)

        self.assertIsNotNone(request)
        self.assertEqual(request.status, 'pending')
        self.assertTrue(
            HITLRequest.objects.filter(user=self.user, status='pending').exists()
        )

    @patch('notifications.reminders.push_device_notification')
    def test_off_is_excluded_from_the_hourly_nudge(self, _push):
        quiet = self.execution(self.agent(notifyOnHitl=False), thread_id='t-quiet')
        self.open(quiet, call_id='c-quiet')

        self.assertEqual(pending_for_nudges(self.user).count(), 0)

    @patch('notifications.reminders.push_device_notification')
    def test_on_is_included_in_the_hourly_nudge(self, _push):
        loud = self.execution(self.agent(notifyOnHitl=True), thread_id='t-loud')
        self.open(loud, call_id='c-loud')

        self.assertEqual(pending_for_nudges(self.user).count(), 1)

    @patch('notifications.reminders.push_device_notification')
    def test_a_missing_key_is_included_in_the_hourly_nudge(self, _push):
        """The `exclude(...=False)` trap, pinned.

        On a JSON key path `NOT (key = False)` is NULL when the key is absent,
        and NULL is not TRUE — so the obvious spelling drops exactly the agents
        that never opted out.
        """
        legacy = self.execution(self.agent(), thread_id='t-legacy')
        self.open(legacy, call_id='c-legacy')

        self.assertEqual(pending_for_nudges(self.user).count(), 1)

    @patch('notifications.reminders.push_device_notification')
    def test_a_quiet_agent_still_counts_in_the_daily_digest(self, _push):
        """The digest is a roll-up, and a roll-up that hides work is a lie.

        `notifyOnHitl` suppresses pushes; the digest is a once-a-day summary the
        user opted into separately, so it reads `HITLRequest` unfiltered.
        """
        quiet = self.execution(self.agent(notifyOnHitl=False))
        self.open(quiet)

        self.assertEqual(
            HITLRequest.objects.filter(user=self.user, status='pending').count(), 1
        )


class ResolutionTests(AgentHITLTestCase):
    """Answering a call has to close its queue entry, or it nudges for ever."""

    def resolve(self, thread_id, call_id, status):
        from agents.agent.hitl import resolve_request

        async_to_sync(resolve_request)(
            thread_id=thread_id, call_id=call_id,
            user_id=self.user.id, status=status,
        )

    @patch('notifications.reminders.push_device_notification')
    def test_approving_closes_the_request(self, _push):
        log = self.execution(self.agent(), thread_id='t-1')
        request = self.open(log, call_id='c-1')

        self.resolve('t-1', 'c-1', 'approved')

        request.refresh_from_db()
        self.assertEqual(request.status, 'approved')
        self.assertIsNotNone(request.responded_at)

    @patch('notifications.reminders.push_device_notification')
    def test_rejecting_closes_the_request(self, _push):
        log = self.execution(self.agent(), thread_id='t-2')
        request = self.open(log, call_id='c-2')

        self.resolve('t-2', 'c-2', 'rejected')

        request.refresh_from_db()
        self.assertEqual(request.status, 'rejected')

    @patch('notifications.reminders.push_device_notification')
    def test_closing_it_cancels_the_reminder_ladder(self, _push):
        """The reason resolution matters at all: an answered question that goes
        on nudging is worse than one that never nudged."""
        log = self.execution(self.agent(), thread_id='t-3')
        request = self.open(log, call_id='c-3')
        # Armed: `next_due_at` is the only field the sweep queries, so a live
        # ladder is one with a due time and a cancelled one is NULL.
        self.assertTrue(
            HITLReminderSchedule.objects.filter(
                hitl_request=request, next_due_at__isnull=False
            ).exists()
        )

        self.resolve('t-3', 'c-3', 'approved')

        schedule = HITLReminderSchedule.objects.get(hitl_request=request)
        self.assertIsNone(schedule.next_due_at)

    @patch('notifications.reminders.push_device_notification')
    def test_it_leaves_another_users_request_alone(self, _push):
        """A thread id is a string, not an authorisation."""
        log = self.execution(self.agent(), thread_id='t-4')
        request = self.open(log, call_id='c-4')

        stranger = User.objects.create_user(username='stranger', password='pw')
        async_to_sync(
            __import__('agents.agent.hitl', fromlist=['resolve_request']).resolve_request
        )(thread_id='t-4', call_id='c-4', user_id=stranger.id, status='approved')

        request.refresh_from_db()
        self.assertEqual(request.status, 'pending')

    @patch('notifications.reminders.push_device_notification')
    def test_resolving_something_that_was_never_queued_is_quiet(self, _push):
        """Normal for a chat approval, and for runs that paused before the
        queue existed. Not an error."""
        self.resolve('no-such-thread', 'no-such-call', 'approved')


class ChatIsUnaffectedTests(TestCase):
    """Chat has no `ExecutionLog`, so it keeps its own inline notification."""

    def test_a_chat_turn_does_not_claim_to_have_a_queue(self):
        from chat.turn.agent import TurnContext

        turn = TurnContext(
            provider='openrouter', model='m', system_message='s',
            user_id=1, session_id='s1', intent='chat', user_text='hi',
        )
        self.assertFalse(turn.approval_queue)

    def test_the_agent_runtime_is_what_turns_it_on(self):
        """Pins the flag to a real construction site rather than a default.

        If the runtime stopped setting it, agent pauses would silently go back
        to the ad-hoc notification that ignores `notifyOnHitl` — the exact
        behaviour the "Coming soon" copy was apologising for.
        """
        import inspect

        from agents.agent import runtime

        self.assertIn('approval_queue=True', inspect.getsource(runtime))


class InboxTests(AgentHITLTestCase):
    """The queue is what the Inbox reads; before this it was always empty."""

    @patch('notifications.reminders.push_device_notification')
    def test_a_paused_run_appears_in_the_pending_endpoint(self, _push):
        from django.urls import reverse
        from rest_framework.test import APIClient

        log = self.execution(self.agent())
        self.open(log)

        client = APIClient()
        client.force_authenticate(user=self.user)
        response = client.get(reverse('orchestrator:pending_hitl'))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['count'], 1)

    @patch('notifications.reminders.push_device_notification')
    def test_an_answered_run_leaves_the_pending_endpoint(self, _push):
        from django.urls import reverse
        from rest_framework.test import APIClient

        log = self.execution(self.agent(), thread_id='t-9')
        self.open(log, call_id='c-9')
        from agents.agent.hitl import resolve_request

        async_to_sync(resolve_request)(
            thread_id='t-9', call_id='c-9', user_id=self.user.id,
            status='approved',
        )

        client = APIClient()
        client.force_authenticate(user=self.user)
        response = client.get(reverse('orchestrator:pending_hitl'))

        self.assertEqual(response.data['count'], 0)


class NoDoubleNotificationTests(AgentHITLTestCase):
    """One pause, one announcement."""

    @patch('notifications.reminders.push_device_notification')
    def test_an_agent_pause_notifies_once(self, _push):
        """Stage 0 of the ladder is the announcement.

        `_require_approval` used to write its own row unconditionally; with the
        queue live that would be a second Inbox entry for one question, and the
        ad-hoc one ignores `notifyOnHitl`.
        """
        log = self.execution(self.agent())
        self.open(log)

        self.assertEqual(
            Notification.objects.filter(
                user=self.user, type='hitl_request'
            ).count(),
            1,
        )
