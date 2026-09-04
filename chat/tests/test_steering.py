"""
Steering: saying something to a run that is already going.

The design claim being protected is that this needs no `interrupt()`. A steer
is already in the mailbox when the graph looks, so the graph never waits — and
`run_turn`'s pause detection keeps exactly one reason to fire, instead of two
it would have to tell apart from an approval.
"""
from __future__ import annotations

import uuid
from unittest.mock import patch

from asgiref.sync import async_to_sync
from django.test import SimpleTestCase, TestCase

from chat.turn import steering
from chat.turn.agent import TurnContext, run_turn
from llm.access import Completion, ToolCall


class MailboxTests(SimpleTestCase):
    def setUp(self):
        steering.clear()
        self.addCleanup(steering.clear)

    def test_a_steer_is_delivered_once(self):
        steering.post('run-1', 'focus on pricing')

        self.assertEqual(steering.take('run-1'), 'focus on pricing')
        self.assertEqual(steering.take('run-1'), '')

    def test_taking_from_an_empty_mailbox_is_free_and_falsy(self):
        """Called at every node boundary of every run, steering or not."""
        self.assertEqual(steering.take('nobody'), '')
        self.assertFalse(steering.pending('nobody'))

    def test_every_steer_is_delivered_in_order(self):
        """Steering is usually additive, not corrective.

        This replaced `test_the_last_steer_wins`. The old behaviour dropped all
        but the newest, on the reasoning that two steers mean the user changed
        their mind — true of a correction and false of the normal case, which
        is a person adding to the brief while the agent works. Three
        instructions became one, and the API had reported success for all
        three.
        """
        steering.post('run-1', 'check pricing')
        steering.post('run-1', 'and the changelog')
        steering.post('run-1', 'and their docs')

        delivered = steering.take('run-1')
        self.assertIn('check pricing', delivered)
        self.assertIn('and the changelog', delivered)
        self.assertIn('and their docs', delivered)
        # Order is legible to the model, so a later instruction can refine an
        # earlier one — which is how a correction still works without the
        # mailbox having to guess which kind of message it is holding.
        self.assertLess(delivered.index('check pricing'),
                        delivered.index('and their docs'))
        self.assertEqual(steering.stats('run-1')['delivered'], 3)

    def test_one_steer_is_delivered_verbatim(self):
        """The common case must not grow scaffolding it does not need."""
        steering.post('run-1', 'focus on pricing')
        self.assertEqual(steering.take('run-1'), 'focus on pricing')

    def test_the_queue_is_drained_completely(self):
        steering.post('run-1', 'one')
        steering.post('run-1', 'two')
        steering.take('run-1')

        self.assertEqual(steering.take('run-1'), '')
        self.assertFalse(steering.pending('run-1'))

    def test_an_overfull_queue_drops_the_oldest_and_counts_it(self):
        """Newest wins on overflow, and the loss is reported.

        A mailbox this full means the run is not reaching a boundary, so the
        instruction the user is actually waiting on is the most recent one.
        Reporting the drop matters more than the policy: an instruction the
        user believes was accepted and that silently vanished is the exact
        failure this module exists to prevent.
        """
        for i in range(steering.MAX_QUEUED_STEERS + 3):
            steering.post('run-1', f'message {i}')

        stats = steering.stats('run-1')
        self.assertEqual(stats['queued'], steering.MAX_QUEUED_STEERS)
        self.assertEqual(stats['dropped'], 3)

        delivered = steering.take('run-1')
        self.assertNotIn('message 0', delivered)
        self.assertIn(f'message {steering.MAX_QUEUED_STEERS + 2}', delivered)

    def test_a_batch_is_bounded_even_when_each_steer_is_legal(self):
        """Eight legal steers are still eight times the cost.

        The batch lands as one user message that every later turn of the run
        pays for again, so the per-message cap cannot be the only bound.
        """
        for i in range(steering.MAX_QUEUED_STEERS):
            steering.post('run-1', 'x' * steering.MAX_STEER_CHARS)

        delivered = steering.take('run-1')
        self.assertLessEqual(len(delivered), steering.MAX_BATCH_CHARS + 200)
        self.assertIn('dropped to fit', delivered)

    def test_an_empty_steer_is_refused(self):
        self.assertFalse(steering.post('run-1', '   '))
        self.assertFalse(steering.pending('run-1'))

    def test_an_oversized_steer_is_truncated_and_says_so(self):
        steering.post('run-1', 'x' * (steering.MAX_STEER_CHARS + 500))

        delivered = steering.take('run-1')
        self.assertIn('[truncated]', delivered)
        self.assertLess(len(delivered), steering.MAX_STEER_CHARS + 100)

    def test_discard_drops_the_slot(self):
        steering.post('run-1', 'hello')
        steering.discard('run-1')

        self.assertFalse(steering.pending('run-1'))


class _Harness:
    """A real turn with a scripted model and a counting dispatcher."""

    def __init__(self, *scripted: Completion):
        self.scripted = list(scripted)
        self.seen: list[list] = []
        self.prompts: list[str] = []
        self.thread_id = f'steer-{uuid.uuid4()}'

    async def model(self, turn, *, prompt, history, tools, **_):
        self.seen.append(list(history))
        self.prompts.append(prompt)
        return self.scripted.pop(0) if self.scripted else Completion(content='done')

    async def dispatch(self, name, args, context):
        return f'{name} ran'

    async def tools(self):
        return []

    def context(self) -> TurnContext:
        return TurnContext(
            provider='openrouter', model='test/model', system_message='',
            user_id=1, session_id=self.thread_id, intent='chat',
            user_text='go', memory_enabled=False,
            tool_source=self.tools, tool_dispatch=self.dispatch,
            sensitive_tools=frozenset(),
        )

    def run(self):
        with patch('chat.turn.agent._run_model', self.model):
            return async_to_sync(run_turn)(
                self.context(), prompt='go', thread_id=self.thread_id,
            )


def _tool_turn(name: str = 'web_search') -> Completion:
    return Completion(
        content='',
        tool_calls=(ToolCall(id=f'c-{name}', name=name, arguments={'query': 'x'}),),
    )


class SteeringInATurnTests(TestCase):
    def setUp(self):
        steering.clear()
        self.addCleanup(steering.clear)

    def test_a_steer_reaches_the_model_on_the_next_boundary(self):
        harness = _Harness(_tool_turn(), Completion(content='ok'))
        steering.post(harness.thread_id, 'actually, focus on pricing')

        harness.run()

        # `_split_transcript` peels a trailing user message off as the prompt,
        # which is exactly where a new instruction belongs: the request is
        # built as [system] + history + [prompt], so the steer is the last
        # thing the model reads rather than something buried mid-transcript.
        self.assertEqual(harness.prompts[-1], 'actually, focus on pricing')

    def test_the_steer_arrives_as_a_user_message(self):
        harness = _Harness(_tool_turn(), Completion(content='ok'))
        steering.post(harness.thread_id, 'focus on pricing')

        harness.run()

        # It is the user talking, so it arrives as the user turn — the model
        # already knows how to weigh a later instruction against an earlier one.
        self.assertEqual(harness.prompts[-1], 'focus on pricing')
        self.assertNotIn('pricing', str(harness.seen[-1]))

    def test_a_steer_does_not_separate_a_tool_call_from_its_results(self):
        """The transcript must stay well-formed or providers reject it.

        The steer node sits after `tools`, so the `tool` messages answering the
        assistant's call ids are already threaded when the steer lands.
        """
        harness = _Harness(_tool_turn(), Completion(content='ok'))
        steering.post(harness.thread_id, 'focus on pricing')

        harness.run()

        history = harness.seen[-1]
        roles = [m.get('role') for m in history]
        assistant_at = roles.index('assistant')
        # The message straight after the tool-calling assistant turn is its
        # tool result, never the steer.
        self.assertEqual(roles[assistant_at + 1], 'tool')

    def test_a_turn_with_no_steer_is_completely_unaffected(self):
        harness = _Harness(_tool_turn(), Completion(content='ok'))

        result = harness.run()

        self.assertEqual(result.answer, 'ok')
        # With no steer the transcript ends on tool output, so the trailing
        # turn is the standing continuation nudge, not a user message.
        self.assertNotEqual(harness.prompts[-1], 'go')
        self.assertEqual(
            [m for m in harness.seen[-1] if m.get('role') == 'user'][0]['content'],
            'go',
        )

    def test_a_steer_is_consumed_and_not_repeated_every_round(self):
        harness = _Harness(
            _tool_turn('a'), _tool_turn('b'), Completion(content='ok'),
        )
        steering.post(harness.thread_id, 'focus on pricing')

        harness.run()

        # Delivered on the first boundary only; the last round must not see it
        # again as a fresh instruction.
        self.assertEqual(harness.prompts[1], 'focus on pricing')
        self.assertNotEqual(harness.prompts[2], 'focus on pricing')

    def test_steering_does_not_make_the_turn_look_paused(self):
        """The whole reason this is a plain node and not an `interrupt()`."""
        harness = _Harness(_tool_turn(), Completion(content='ok'))
        steering.post(harness.thread_id, 'focus on pricing')

        result = harness.run()

        self.assertFalse(result.awaiting_approval)
