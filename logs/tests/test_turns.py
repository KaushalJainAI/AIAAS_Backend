"""
The turn is recorded, and it holds the reasoning.

These are the tests for the fact this refactor exists to establish: an agent run
is a loop of turns, each turn has reasoning behind it, and every tool call
belongs to the turn that issued it. Before, the grouping lived in a JSON blob
reassembled at read time and the reasoning was `thinking[-150:]`, discarded when
the run closed.
"""
from unittest.mock import AsyncMock

from asgiref.sync import async_to_sync
from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone

from agents.agent.stream import AgentRunStream
from agents.models import SubAgent
from chat.turn.events import Event
from logs.models import AgentStep, AgentTurn, ExecutionLog
from workflow_backend.thresholds import (
    TURN_CONTENT_CHAR_LIMIT,
    TURN_REASONING_CHAR_LIMIT,
)


class TurnRecordingTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='turner', password='pw')
        self.agent = SubAgent.objects.create(user=self.user, name='Researcher')
        self.log = ExecutionLog.objects.create(
            user=self.user, subagent=self.agent, status='running',
            input_data={'goal': 'Find it', 'thread_id': 't-1'},
            started_at=timezone.now(),
        )
        self.stream = AgentRunStream(self.log, broadcaster=AsyncMock())

    def _turn(self, index, **kwargs):
        defaults = dict(
            reasoning='', content='', decision='tools', provider='openrouter',
            model_id='anthropic/claude-opus-5', tokens=100, duration_ms=50,
        )
        defaults.update(kwargs)
        async_to_sync(self.stream.on_model_turn)(index=index, **defaults)

    def _call(self, call_id, tool='web_search', status='completed'):
        async_to_sync(self.stream.sink)(Event.AGENT_TRACE, {
            'sub_type': 'tool', 'tool': tool, 'args': {}, 'call_id': call_id,
        })
        async_to_sync(self.stream.on_tool_result)(
            call_id=call_id, name=tool, args={}, output='ok',
            status=status, duration_ms=5,
        )

    def test_one_row_per_model_call_with_its_own_reasoning(self):
        self._turn(1, reasoning='First I search.')
        self._turn(2, reasoning='Now I read the best hit.')

        turns = list(AgentTurn.objects.filter(execution=self.log).order_by('index'))
        self.assertEqual([t.index for t in turns], [1, 2])
        # Each turn holds its own reasoning, not the running total. Handing the
        # observer the accumulated `thinking` would make turn 2 a superset of
        # turn 1 and attribute the first turn's thoughts to the second.
        self.assertEqual(turns[0].reasoning, 'First I search.')
        self.assertEqual(turns[1].reasoning, 'Now I read the best hit.')

    def test_the_model_that_served_each_turn_is_recorded(self):
        self._turn(1, provider='nvidia', model_id='nvidia/nemotron')
        turn = AgentTurn.objects.get(execution=self.log, index=1)
        self.assertEqual(turn.provider, 'nvidia')
        self.assertEqual(turn.model_id, 'nvidia/nemotron')
        self.assertEqual(turn.tokens, 100)

    def test_calls_issued_together_share_a_turn(self):
        """Grouping is the whole difference between a loop and a pipeline."""
        self._turn(1, reasoning='Search two ways at once.')
        self._call('a')
        self._call('b')
        self._turn(2, reasoning='Now read.')
        self._call('c', tool='read_url')

        first = AgentTurn.objects.get(execution=self.log, index=1)
        second = AgentTurn.objects.get(execution=self.log, index=2)
        self.assertEqual(
            sorted(s.call_id for s in first.steps.all()), ['a', 'b']
        )
        self.assertEqual([s.call_id for s in second.steps.all()], ['c'])

    def test_a_turn_before_any_tool_call_leaves_steps_unattributed(self):
        """A step whose turn never got written is still recorded."""
        self._call('orphan')
        step = AgentStep.objects.get(execution=self.log, call_id='orphan')
        self.assertIsNone(step.turn_id)

    def test_reasoning_is_capped_and_the_cut_is_marked(self):
        self._turn(1, reasoning='x' * (TURN_REASONING_CHAR_LIMIT + 500))
        turn = AgentTurn.objects.get(execution=self.log, index=1)
        self.assertEqual(len(turn.reasoning), TURN_REASONING_CHAR_LIMIT)
        # A trimmed thought and a genuinely brief one must not look alike.
        self.assertTrue(turn.reasoning_truncated)

    def test_short_reasoning_is_not_marked_as_truncated(self):
        self._turn(1, reasoning='Brief.')
        turn = AgentTurn.objects.get(execution=self.log, index=1)
        self.assertFalse(turn.reasoning_truncated)

    def test_content_is_capped_independently(self):
        self._turn(1, content='y' * (TURN_CONTENT_CHAR_LIMIT + 10), decision='answer')
        turn = AgentTurn.objects.get(execution=self.log, index=1)
        self.assertEqual(len(turn.content), TURN_CONTENT_CHAR_LIMIT)
        self.assertTrue(turn.content_truncated)

    def test_a_resumed_turn_updates_rather_than_duplicating(self):
        """`(execution, index)` is unique: a second row would double the tokens."""
        self._turn(1, reasoning='first pass', tokens=100)
        self._turn(1, reasoning='after approval', tokens=140)

        turns = AgentTurn.objects.filter(execution=self.log, index=1)
        self.assertEqual(turns.count(), 1)
        self.assertEqual(turns.first().reasoning, 'after approval')
        self.assertEqual(turns.first().tokens, 140)

    def test_the_last_turns_decision_reflects_how_the_run_ended(self):
        self._turn(1, decision='tools')
        async_to_sync(self.stream.run_finished)(
            status='paused', answer='', duration_ms=10,
        )
        self.assertEqual(
            AgentTurn.objects.get(execution=self.log, index=1).decision, 'paused'
        )

    def test_a_failing_observer_never_fails_the_run(self):
        """Watching a run may not break it."""
        broken = AgentRunStream(self.log, broadcaster=AsyncMock())
        broken._record_turn = AsyncMock(side_effect=RuntimeError('db down'))

        with self.assertLogs('agents.agent.stream', level='ERROR'):
            async_to_sync(broken.on_model_turn)(
                index=1, reasoning='r', content='', decision='tools',
                provider='p', model_id='m', tokens=1, duration_ms=1,
            )

        # And the steps that follow are unattributed rather than lost.
        self.assertIsNone(broken._turn_id)


class StepLifecycleTests(TestCase):
    """A step is opened when the call starts, then closed by its result."""

    def setUp(self):
        self.user = User.objects.create_user(username='stepper', password='pw')
        self.agent = SubAgent.objects.create(user=self.user, name='Worker')
        self.log = ExecutionLog.objects.create(
            user=self.user, subagent=self.agent, status='running',
            input_data={'goal': 'g', 'thread_id': 't'}, started_at=timezone.now(),
        )
        self.stream = AgentRunStream(self.log, broadcaster=AsyncMock())

    def test_a_running_step_is_visible_before_the_tool_returns(self):
        async_to_sync(self.stream.sink)(Event.AGENT_TRACE, {
            'sub_type': 'tool', 'tool': 'deep_research', 'args': {'q': 'x'},
            'call_id': 'c1',
        })
        step = AgentStep.objects.get(execution=self.log, call_id='c1')
        self.assertEqual(step.status, 'running')
        self.assertEqual(step.args, {'args': {'q': 'x'}})
        self.assertIsNotNone(step.started_at)

    def test_the_result_closes_the_same_row(self):
        async_to_sync(self.stream.sink)(Event.AGENT_TRACE, {
            'sub_type': 'tool', 'tool': 'web_search', 'args': {}, 'call_id': 'c1',
        })
        async_to_sync(self.stream.on_tool_result)(
            call_id='c1', name='web_search', args={}, output='found',
            status='completed', duration_ms=7,
        )
        steps = AgentStep.objects.filter(execution=self.log)
        self.assertEqual(steps.count(), 1)
        step = steps.first()
        self.assertEqual(step.status, 'completed')
        self.assertEqual(step.result, {'result': 'found'})
        self.assertEqual(step.duration_ms, 7)
        self.assertIsNotNone(step.completed_at)

    def test_a_failure_is_recorded_with_its_error(self):
        async_to_sync(self.stream.sink)(Event.AGENT_TRACE, {
            'sub_type': 'tool', 'tool': 'read_url', 'args': {}, 'call_id': 'c1',
        })
        async_to_sync(self.stream.on_tool_result)(
            call_id='c1', name='read_url', args={}, output='403 from origin',
            status='failed', duration_ms=2,
        )
        step = AgentStep.objects.get(execution=self.log, call_id='c1')
        self.assertEqual(step.status, 'failed')
        self.assertEqual(step.error_message, '403 from origin')
