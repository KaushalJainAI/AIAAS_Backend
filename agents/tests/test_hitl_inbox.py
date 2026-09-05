"""
Answering a paused run from the Inbox.

`respond_to_hitl` used to set `status`, write `responded_at` and return 200 —
and stop. Its own comment said so, deferring the resume to
`agents/{id}/approve/`, a route the Inbox has never called. So the screen was a
dead end that looked like it worked: the toast said "Response sent", the row
left the queue, the reminder ladder stood down because nothing is pending any
more, and the agent stayed parked on its `interrupt()` with nobody left to
notice. That is worse than an error, because the one person who could have
rescued the run has just been told it is handled.

These tests are about the seam rather than the happy path: what happens when
the same pause is answered twice, when the row cannot be resumed, and when the
resume itself fails.
"""
from __future__ import annotations

from unittest.mock import patch

from django.contrib.auth.models import User
from rest_framework.test import APITestCase

from agents.models import HITLRequest, SubAgent
from logs.models import ExecutionLog


class InboxResponseTestCase(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='owner', password='pw')
        self.client.force_authenticate(user=self.user)
        self.agent = SubAgent.objects.create(user=self.user, name='Reporter')
        self.log = ExecutionLog.objects.create(
            user=self.user, subagent=self.agent, status='paused',
            thread_id='thread-1',
            input_data={'thread_id': 'thread-1', 'goal': 'do the thing'},
        )

    def request(self, **context) -> HITLRequest:
        data = {'tool': 'send_email', 'call_id': 'call-1',
                'thread_id': 'thread-1', 'agent_id': self.agent.id}
        data.update(context)
        return HITLRequest.objects.create(
            user=self.user, execution=self.log, node_id=data.get('call_id') or '',
            request_type='approval', title='Send email · Gmail',
            message='Send email using your Gmail connection.',
            context_data=data,
        )

    def respond(self, request_id, **body):
        return self.client.post(f'/api/orchestrator/hitl/{request_id}/respond/',
                                body, format='json')


class ApprovalResumesTheRun(InboxResponseTestCase):
    def test_approving_records_consent_and_resumes(self):
        row = self.request()

        with patch('chat.turn.agent.approve_tool_call') as approve, \
             patch('agents.agent.runtime.resume_agent_run') as resume:
            resume.return_value = 'exec-9'
            response = self.respond(row.request_id, action='approve')

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data['resumed'])
        self.assertEqual(response.data['execution_id'], 'exec-9')

        approve.assert_awaited_once()
        self.assertEqual(approve.await_args.args, ('thread-1', 'call-1'))
        resume.assert_awaited_once()

        row.refresh_from_db()
        self.assertEqual(row.status, 'approved')
        self.assertIsNotNone(row.responded_at)

    def test_the_scope_reaches_the_allowance(self):
        """"Always allow" from the Inbox has to mean the same thing it means
        in chat, or the two screens grant different things by the same name."""
        row = self.request()

        # `resume_agent_run` is async, so an unset return value hands back an
        # AsyncMock whose children are themselves coroutines — which DRF's
        # encoder then tries to serialise. Pin it.
        with patch('chat.turn.agent.approve_tool_call') as approve, \
             patch('agents.agent.runtime.resume_agent_run') as resume:
            resume.return_value = 'exec-9'
            self.respond(row.request_id, action='approve', scope='always')

        self.assertEqual(approve.await_args.kwargs['scope'], 'always')
        # An agent run's thread id is its session id, but it is passed rather
        # than assumed — relying on them coinciding is what breaks chat.
        self.assertEqual(approve.await_args.kwargs['session_key'], 'thread-1')

    def test_rejecting_resumes_past_the_call(self):
        row = self.request()

        with patch('chat.turn.agent.reject_tool_call') as reject, \
             patch('agents.agent.runtime.resume_agent_run') as resume:
            reject.return_value = True
            resume.return_value = 'exec-9'
            response = self.respond(row.request_id, action='reject',
                                    message='not that mailbox')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(reject.await_args.kwargs['reason'], 'not that mailbox')
        resume.assert_awaited_once()
        row.refresh_from_db()
        self.assertEqual(row.status, 'rejected')

    def test_a_rejection_with_no_graph_state_does_not_resume(self):
        """`reject_tool_call` returns False when the thread has no state.

        Resuming anyway would reopen a log for a run that is not paused."""
        row = self.request()

        with patch('chat.turn.agent.reject_tool_call') as reject, \
             patch('agents.agent.runtime.resume_agent_run') as resume:
            reject.return_value = False
            response = self.respond(row.request_id, action='reject')

        resume.assert_not_awaited()
        self.assertFalse(response.data['resumed'])
        # The decision is still recorded, so it leaves the queue.
        row.refresh_from_db()
        self.assertEqual(row.status, 'rejected')


class AnsweredOnceTests(InboxResponseTestCase):
    def test_answering_twice_resumes_once(self):
        """The live socket and the Inbox can both reach the same pause.

        The `status='pending'` filter is the whole guard, and it had never been
        exercised because nothing resumed in the first place.
        """
        row = self.request()

        with patch('chat.turn.agent.approve_tool_call'), \
             patch('agents.agent.runtime.resume_agent_run') as resume:
            resume.return_value = 'exec-9'
            first = self.respond(row.request_id, action='approve')
            second = self.respond(row.request_id, action='approve')

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 404)
        resume.assert_awaited_once()

    def test_another_users_request_is_not_found(self):
        row = self.request()
        other = User.objects.create_user(username='stranger', password='pw')
        self.client.force_authenticate(user=other)

        with patch('agents.agent.runtime.resume_agent_run') as resume:
            response = self.respond(row.request_id, action='approve')

        self.assertEqual(response.status_code, 404)
        resume.assert_not_awaited()
        row.refresh_from_db()
        self.assertEqual(row.status, 'pending')


class RowsThatCannotResumeTests(InboxResponseTestCase):
    """Closing the row is unconditional; only the resume is conditional."""

    def test_a_row_with_no_thread_id_still_closes(self):
        """Written before `context_data` carried one. Answering it must not
        500 on a key that was never there."""
        row = self.request(thread_id='')

        with patch('agents.agent.runtime.resume_agent_run') as resume:
            response = self.respond(row.request_id, action='approve')

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.data['resumed'])
        resume.assert_not_awaited()
        row.refresh_from_db()
        self.assertEqual(row.status, 'approved')

    def test_a_clarification_answer_is_recorded_and_not_dispatched(self):
        """An answer to a question is not a decision about a tool call, so
        there is nothing to write into the checkpoint for it."""
        row = self.request()
        row.request_type = 'clarification'
        row.save(update_fields=['request_type'])

        with patch('chat.turn.agent.approve_tool_call') as approve, \
             patch('agents.agent.runtime.resume_agent_run') as resume:
            response = self.respond(row.request_id, action='respond',
                                    response='the second one')

        approve.assert_not_awaited()
        resume.assert_not_awaited()
        self.assertEqual(response.status_code, 200)
        row.refresh_from_db()
        self.assertEqual(row.status, 'answered')
        self.assertEqual(row.response['value'], 'the second one')

    def test_a_failed_resume_does_not_lose_the_answer(self):
        """The user has no second copy of their decision to send.

        Failing the request here would tell them it did not land when it did.
        """
        row = self.request()

        with patch('chat.turn.agent.approve_tool_call'), \
             patch('agents.agent.runtime.resume_agent_run',
                   side_effect=RuntimeError('boom')):
            response = self.respond(row.request_id, action='approve')

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.data['resumed'])
        row.refresh_from_db()
        self.assertEqual(row.status, 'approved')


class QueueRowContentTests(InboxResponseTestCase):
    """What `open_request` writes is what the Inbox renders."""

    def test_the_row_names_the_call_rather_than_its_encoded_name(self):
        from asgiref.sync import async_to_sync

        from agents.agent.hitl import open_request
        from chat.tools.describe import describe_call

        detail = describe_call('mcp__7__send_email_ab12cd34',
                               {'to': 'priya@acme.test'}, server='Gmail')
        with self.captureOnCommitCallbacks(execute=True):
            async_to_sync(open_request)(
                self.log, call_id='call-2', tool='mcp__7__send_email_ab12cd34',
                message=f"{detail['sentence']} It will not run until you approve it.",
                title=detail['title'], detail=detail, args={'to': 'priya@acme.test'},
            )

        row = HITLRequest.objects.get(node_id='call-2')
        self.assertEqual(row.title, 'Send email · Gmail')
        self.assertNotIn('mcp__', row.title)
        self.assertNotIn('mcp__', row.message)
        self.assertEqual(row.context_data['detail']['fields'],
                         [{'label': 'To', 'value': 'priya@acme.test'}])

    def test_the_serializer_exposes_the_detail_and_not_the_routing(self):
        from agents.serializers import HITLRequestSerializer

        row = self.request()
        row.context_data['detail'] = {'title': 'Send email · Gmail', 'fields': []}
        row.save(update_fields=['context_data'])

        data = HITLRequestSerializer(row).data
        self.assertEqual(data['detail']['title'], 'Send email · Gmail')
        # `context_data` also holds the thread id and the agent id. The client
        # needs no part of that, so the column is not exposed.
        self.assertNotIn('context_data', data)

    def test_a_row_written_before_describe_existed_has_no_detail(self):
        from agents.serializers import HITLRequestSerializer

        data = HITLRequestSerializer(self.request()).data
        self.assertIsNone(data['detail'])
