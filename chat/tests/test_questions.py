"""
Questions and a manager answering its workers (2026-09-25).

Pinned, through the real graph where it matters:

* `ask_user` pauses a chat turn on a question card, the answer is checked
  against the question the run asked, and it comes back as the tool's result;
  a skip lets the run proceed on its stated assumption; a run nobody can
  answer (a schedule) never pauses.
* A malformed question is refused as an error, never drawn.
* `answer_subagent`: answering a worker's question or refusing its request is
  the manager's call in any mode; only *approving* can need the person, and
  under `auto` only when the worker's call trips the same floor auto keeps
  for its own calls. The card shows the worker's request, not the manager's
  tool name. A person's answer to that card is the decision.
* `create_agent` / `update_agent` run without asking.
"""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

from asgiref.sync import async_to_sync
from django.contrib.auth.models import User
from django.test import SimpleTestCase, TestCase

from agents.models import HITLRequest, SubAgent
from chat.tools.ask import ask_user, normalise_answer, question_spec
from chat.turn.agent import TurnContext, answer_question, reject_tool_call, run_turn
from chat.turn.events import Event
from logs.models import ExecutionLog


class QuestionSpecTests(SimpleTestCase):
    def test_options_make_a_choice(self):
        spec, _ = question_spec({'question': 'Which report?', 'assumption': 'weekly',
                                 'options': ['Weekly', 'Monthly', 'Weekly']})
        self.assertEqual(spec['kind'], 'choice')
        self.assertEqual(spec['options'], ['Weekly', 'Monthly'])

    def test_a_choice_needs_two_to_eight_options(self):
        spec, problem = question_spec({'question': 'Q?', 'assumption': 'a',
                                       'kind': 'choice', 'options': ['only']})
        self.assertIsNone(spec)
        self.assertIn('2-8', problem)

    def test_a_question_needs_an_assumption(self):
        spec, problem = question_spec({'question': 'Q?'})
        self.assertIsNone(spec)
        self.assertIn('assumption', problem)

    def test_number_bounds_are_checked(self):
        spec, problem = question_spec({'question': 'Budget?', 'assumption': '1000',
                                       'kind': 'number', 'min': 10, 'max': 1})
        self.assertIsNone(spec)
        spec, _ = question_spec({'question': 'Budget?', 'assumption': '1000',
                                 'kind': 'number', 'min': 0, 'max': 5000, 'unit': 'INR'})
        self.assertEqual(normalise_answer(spec, '1200'), (1200, ''))
        self.assertIsNone(normalise_answer(spec, 9000)[0])
        self.assertIsNone(normalise_answer(spec, 'lots')[0])

    def test_answers_must_fit_the_options(self):
        spec, _ = question_spec({'question': 'Q?', 'assumption': 'a',
                                 'options': ['A', 'B']})
        self.assertEqual(normalise_answer(spec, 'B'), ('B', ''))
        self.assertIsNone(normalise_answer(spec, 'C')[0])
        spec['allow_other'] = True
        self.assertEqual(normalise_answer(spec, 'C'), ('C', ''))

    def test_multi_choice_takes_a_list(self):
        spec, _ = question_spec({'question': 'Q?', 'assumption': 'a',
                                 'kind': 'multi_choice', 'options': ['A', 'B', 'C']})
        self.assertEqual(normalise_answer(spec, ['A', 'C']), (['A', 'C'], ''))

    def test_the_tool_returns_the_answer_or_the_assumption(self):
        args = {'question': 'Which?', 'assumption': 'Weekly', 'options': ['Weekly', 'Monthly']}
        answered = json.loads(async_to_sync(ask_user)(
            args, {'answered': True, 'user_answer': 'Monthly'}))
        self.assertEqual(answered['answer'], 'Monthly')
        unanswered = json.loads(async_to_sync(ask_user)(args, {}))
        self.assertIsNone(unanswered['answer'])
        self.assertIn('Weekly', unanswered['note'])


class _Scripted:
    """A model that asks one question, then answers; records what it was sent."""

    def __init__(self, question: dict) -> None:
        self.question = question
        self.turn = 0
        self.sent: list = []

    def __call__(self, **kwargs):
        self.sent.append(kwargs)
        index = self.turn
        self.turn += 1

        async def chunks():
            if index == 0:
                yield {"type": "tool_calls", "tool_calls": [{
                    "index": 0, "id": "q-1",
                    "function": {"name": "ask_user", "arguments": json.dumps(self.question)},
                }]}
            else:
                yield {"type": "content", "content": "done"}
            yield {"type": "metadata", "usage": {"total_tokens": 5}}

        return chunks()


async def _never(name, args, context) -> bool:
    return False


QUESTION = {'question': 'Which report?', 'assumption': 'Weekly',
            'kind': 'choice', 'options': ['Weekly', 'Monthly']}


class QuestionPausesTheTurnTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user('asker', 'a@example.com', 'x')

    def _turn(self, thread, seen, *, can_ask=True):
        async def sink(event, payload):
            seen.append((str(event), payload))

        return TurnContext(
            provider='stub', model='stub-model', system_message='test',
            user_id=self.user.id, session_id=thread, intent='chat', user_text='go',
            memory_enabled=False, max_iterations=6, approval_policy=_never,
            sink=sink, can_ask=can_ask,
        )

    def _go(self, model, turn, thread):
        with patch('llm.access.stream', new=model):
            return async_to_sync(run_turn)(turn, prompt='go', thread_id=thread)

    def _tool_text(self, model) -> str:
        return json.dumps(model.sent[-1], default=str)

    def test_a_question_pauses_and_the_answer_comes_back(self):
        model, seen = _Scripted(QUESTION), []
        first = self._go(model, self._turn('q-thread-1', seen), 'q-thread-1')
        self.assertTrue(first.awaiting_approval)
        frame = next(p for n, p in seen if n == Event.ASK_QUESTION)
        self.assertEqual(frame['options'], ['Weekly', 'Monthly'])
        self.assertEqual(frame['call_id'], 'q-1')

        # Checked against the question the run asked, not the client.
        self.assertFalse(async_to_sync(answer_question)('q-thread-1', 'q-1', 'Yearly')[0])
        self.assertTrue(async_to_sync(answer_question)('q-thread-1', 'q-1', 'Monthly')[0])

        second = self._go(model, self._turn('q-thread-1', seen), 'q-thread-1')
        self.assertFalse(second.awaiting_approval)
        self.assertIn('Monthly', self._tool_text(model))
        self.assertEqual(second.answer, 'done')

    def test_a_skip_proceeds_on_the_assumption(self):
        model, seen = _Scripted(QUESTION), []
        self._go(model, self._turn('q-thread-2', seen), 'q-thread-2')
        async_to_sync(reject_tool_call)('q-thread-2', 'q-1', reason='skipped')
        second = self._go(model, self._turn('q-thread-2', seen), 'q-thread-2')
        self.assertFalse(second.awaiting_approval)
        self.assertIn('skipped this question', self._tool_text(model))

    def test_nobody_to_answer_means_no_pause(self):
        model, seen = _Scripted(QUESTION), []
        result = self._go(model, self._turn('q-thread-3', seen, can_ask=False), 'q-thread-3')
        self.assertFalse(result.awaiting_approval)
        self.assertNotIn(Event.ASK_QUESTION, [n for n, _ in seen])
        self.assertIn('No one can answer', self._tool_text(model))

    def test_a_malformed_question_is_an_error_not_a_card(self):
        model, seen = _Scripted({'question': 'Which?', 'assumption': 'a',
                                 'kind': 'choice', 'options': ['one']}), []
        result = self._go(model, self._turn('q-thread-4', seen), 'q-thread-4')
        self.assertFalse(result.awaiting_approval)
        self.assertIn('2-8', self._tool_text(model))


class ManagerAnswersWorkerTests(TestCase):
    def setUp(self):
        from chat.turn import reviewer

        reviewer._verdicts.clear()
        self.user = User.objects.create_user('boss', 'b@example.com', 'x')
        self.agent = SubAgent.objects.create(user=self.user, name='Mailer')
        self.log = ExecutionLog.objects.create(
            user=self.user, subagent=self.agent, status='paused', thread_id='w-1',
            input_data={'thread_id': 'w-1', 'goal': 'send the report'},
        )

    def row(self, *, kind='approval', call_id='c-1', args=None, tool='send_email'):
        return HITLRequest.objects.create(
            user=self.user, execution=self.log, node_id=call_id, request_type=kind,
            title='Send email · Gmail', message='Send email using your Gmail connection.',
            context_data={'tool': tool, 'call_id': call_id, 'thread_id': 'w-1',
                          'args': args or {},
                          'detail': {'title': 'Send email · Gmail',
                                     'sentence': 'Send email using your Gmail connection.',
                                     'fields': [{'label': 'To', 'value': 'ana@acme.com'}]}},
        )

    def args(self, **extra):
        return {'execution_id': str(self.log.execution_id), 'call_id': 'c-1', **extra}

    def needs(self, args):
        from chat.tools.agents import subagent_answer_needs_user

        return async_to_sync(subagent_answer_needs_user)(args, {'user_id': self.user.id})

    def test_only_approving_can_need_the_person(self):
        self.row()
        self.assertTrue(self.needs(self.args(decision='approve')))
        self.assertFalse(self.needs(self.args(decision='reject')))

    def test_answering_a_question_is_the_managers_call(self):
        self.row(kind='clarification')
        self.assertFalse(self.needs(self.args(answer='Monthly')))

    def test_someone_elses_request_needs_nobody_and_is_refused(self):
        self.row()
        other = User.objects.create_user('other', 'o@example.com', 'x')
        from chat.tools.agents import answer_subagent, subagent_answer_needs_user

        self.assertFalse(async_to_sync(subagent_answer_needs_user)(
            self.args(decision='approve'), {'user_id': other.id}))
        out = json.loads(async_to_sync(answer_subagent)(
            self.args(decision='approve'), {'user_id': other.id}))
        self.assertIn('error', out)

    def test_auto_lets_the_manager_approve_what_auto_could_run(self):
        from chat.turn.reviewer import auto_policy

        self.row(args={'to': 'ana@acme.com', 'subject': 'Report'})
        policy = auto_policy(user_text='email the report to ana@acme.com')
        paused = async_to_sync(policy)('answer_subagent', self.args(decision='approve'),
                                       {'user_id': self.user.id, 'session_id': 's'})
        self.assertFalse(paused)

    def test_auto_still_asks_for_a_recipient_nobody_named(self):
        from chat.turn.reviewer import audit_for, auto_policy

        self.row(args={'to': 'stranger@evil.example'})
        policy = auto_policy(user_text='email the report to ana@acme.com')
        args = self.args(decision='approve')
        paused = async_to_sync(policy)('answer_subagent', args,
                                       {'user_id': self.user.id, 'session_id': 's'})
        self.assertTrue(paused)
        self.assertIn('stranger@evil.example', audit_for(policy, 'answer_subagent', args)['reason'])

    def test_the_card_shows_the_workers_request(self):
        from chat.tools.describe import describe_call_async

        self.row()
        detail = async_to_sync(describe_call_async)('answer_subagent', self.args(decision='approve'))
        self.assertIn('Mailer wants to send email', detail['sentence'])
        self.assertEqual(detail['fields'][0]['value'], 'ana@acme.com')

    def test_approving_records_resolves_and_resumes(self):
        from chat.tools.agents import answer_subagent

        row = self.row()
        with patch('chat.turn.agent.approve_tool_call', new=AsyncMock()) as approve, \
             patch('agents.agent.runtime.resume_agent_run',
                   new=AsyncMock(return_value=str(self.log.execution_id))) as resume, \
             patch('chat.tools.agents._await_agent_run',
                   new=AsyncMock(return_value={'status': 'completed', 'answer': 'sent'})):
            out = json.loads(async_to_sync(answer_subagent)(
                self.args(decision='approve'),
                {'user_id': self.user.id, 'decided_by_user': True}))
        approve.assert_awaited_once()
        resume.assert_awaited_once()
        self.assertEqual(out['request'], 'approved')
        self.assertEqual(out['decided_by'], 'user')
        self.assertEqual(out['answer'], 'sent')
        row.refresh_from_db()
        self.assertEqual(row.status, 'approved')

    def test_a_paused_run_says_what_it_waits_on(self):
        from chat.tools.agents import pending_requests

        self.row()
        waiting = async_to_sync(pending_requests)([str(self.log.execution_id)])
        self.assertEqual(waiting[0]['kind'], 'approval')
        self.assertEqual(waiting[0]['agent'], 'Mailer')
        self.assertIn('Gmail', waiting[0]['request'])
