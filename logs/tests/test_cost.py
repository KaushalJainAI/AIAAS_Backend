"""
What a run cost, from the provider's usage object to the number on the page.

The unit tests in `llm/tests/` prove the arithmetic. These prove the *wiring* —
that the breakdown survives every hop between the provider and the run row,
which is where it was being dropped before: `StreamAccumulator` collapsed the
whole usage object into one integer at the first hop, and nothing downstream
could recover what it had thrown away.
"""
from decimal import Decimal
from unittest.mock import AsyncMock

from asgiref.sync import async_to_sync
from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone

from agents.agent.stream import AgentRunStream
from agents.models import SubAgent
from agents.spend import aggregate_rupees, rupees_for
from llm.models import AIModel, AIProvider
from llm.usage import TokenUsage, normalize
from logs.models import AgentTurn, ExecutionLog


class TurnCostRecordingTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='coster', password='pw')
        self.agent = SubAgent.objects.create(user=self.user, name='Researcher')
        self.log = ExecutionLog.objects.create(
            user=self.user, subagent=self.agent, status='running',
            input_data={'goal': 'Find it', 'thread_id': 't-1'},
            started_at=timezone.now(),
        )
        self.stream = AgentRunStream(self.log, broadcaster=AsyncMock())
        provider = AIProvider.objects.create(name='OpenRouter', slug='openrouter')
        AIModel.objects.create(
            provider=provider, name='Test', value='vendor/model',
            input_price_per_million=Decimal('2.0000'),
            output_price_per_million=Decimal('12.0000'),
            cached_input_price_per_million=Decimal('0.2000'),
        )

    def _turn(self, index, usage, model_id='vendor/model'):
        async_to_sync(self.stream.on_model_turn)(
            index=index, reasoning='', content='', decision='tools',
            provider='openrouter', model_id=model_id,
            tokens=usage.total, duration_ms=50, usage=usage,
        )
        return AgentTurn.objects.get(execution=self.log, index=index)

    def test_the_breakdown_reaches_the_turn_row(self):
        turn = self._turn(1, normalize({
            'prompt_tokens': 1_000_000, 'completion_tokens': 100_000,
            'prompt_tokens_details': {'cached_tokens': 800_000},
        }))
        self.assertEqual(turn.input_tokens, 200_000)
        self.assertEqual(turn.cached_read_tokens, 800_000)
        self.assertEqual(turn.output_tokens, 100_000)
        # 0.2M*$2 + 0.8M*$0.20 + 0.1M*$12 = 0.40 + 0.16 + 1.20
        self.assertEqual(turn.cost_usd, Decimal('1.760000'))
        self.assertEqual(turn.cost_source, 'estimated')

    def test_a_provider_reported_cost_is_recorded_as_billed(self):
        turn = self._turn(1, normalize({
            'prompt_tokens': 100, 'completion_tokens': 10, 'cost': 0.0031,
        }))
        self.assertEqual(turn.cost_source, 'billed')
        self.assertEqual(turn.cost_usd, Decimal('0.003100'))

    def test_an_unpriced_model_records_zero_but_says_so(self):
        turn = self._turn(1, normalize({'prompt_tokens': 100, 'completion_tokens': 10}),
                          model_id='vendor/unknown')
        self.assertEqual(turn.cost_usd, Decimal('0'))
        self.assertEqual(turn.cost_source, 'unpriced')

    def test_an_observer_called_without_usage_still_records_the_turn(self):
        """Chat passes no observer at all; other callers may pass no usage."""
        turn = self._turn(1, TokenUsage())
        self.assertEqual(turn.cost_source, 'unpriced')
        self.assertEqual(turn.input_tokens, 0)

    def test_re_running_a_turn_corrects_its_cost_instead_of_doubling_it(self):
        """A resumed run re-enters at an index it has already used."""
        self._turn(3, normalize({'prompt_tokens': 100, 'completion_tokens': 10,
                                 'cost': 0.005}))
        turn = self._turn(3, normalize({'prompt_tokens': 200, 'completion_tokens': 20,
                                        'cost': 0.009}))
        self.assertEqual(AgentTurn.objects.filter(execution=self.log).count(), 1)
        self.assertEqual(turn.cost_usd, Decimal('0.009000'))


class RunRollupTests(TestCase):
    """The run's totals are the sum of its turns, computed at close."""

    def setUp(self):
        self.user = User.objects.create_user(username='roller', password='pw')
        self.agent = SubAgent.objects.create(user=self.user, name='R')
        self.log = ExecutionLog.objects.create(
            user=self.user, subagent=self.agent, status='running',
            started_at=timezone.now(),
        )

    def _turn(self, index, cost, source='estimated', **tokens):
        AgentTurn.objects.create(
            execution=self.log, index=index, decision='tools',
            model_id='m', cost_usd=Decimal(cost), cost_source=source,
            **tokens,
        )

    def _close(self):
        from agents.agent.runtime import _roll_up_cost
        _roll_up_cost(self.log)
        return self.log

    def test_totals_are_summed_from_the_turns(self):
        self._turn(1, '0.004', input_tokens=100, output_tokens=20)
        self._turn(2, '0.006', input_tokens=300, output_tokens=40,
                   cached_read_tokens=50)
        log = self._close()
        self.assertEqual(log.cost_usd, Decimal('0.010'))
        self.assertEqual(log.input_tokens, 400)
        self.assertEqual(log.output_tokens, 60)
        self.assertEqual(log.cached_read_tokens, 50)
        self.assertEqual(log.cost_source, 'estimated')

    def test_one_unpriced_turn_makes_the_run_unpriced(self):
        self._turn(1, '0.004', source='billed')
        self._turn(2, '0', source='unpriced')
        self.assertEqual(self._close().cost_source, 'unpriced')

    def test_a_run_billed_throughout_stays_billed(self):
        self._turn(1, '0.004', source='billed')
        self._turn(2, '0.001', source='billed')
        self.assertEqual(self._close().cost_source, 'billed')

    def test_a_run_with_no_turns_is_unpriced_rather_than_free(self):
        self.assertEqual(self._close().cost_source, 'unpriced')


class SpendAggregationTests(TestCase):
    """The number the UI shows and the number the cap refuses on are one number."""

    def setUp(self):
        self.user = User.objects.create_user(username='spender', password='pw')
        self.agent = SubAgent.objects.create(user=self.user, name='S')

    def _run(self, **kwargs):
        return ExecutionLog.objects.create(
            user=self.user, subagent=self.agent, status='completed', **kwargs,
        )

    def test_a_priced_run_is_billed_from_its_recorded_cost(self):
        self._run(cost_usd=Decimal('1.00'), cost_source='estimated',
                  tokens_used=999_999_999)
        # 1 USD at 88 INR, not the blended token rate — which for that many
        # tokens would be ~85,000 rupees.
        self.assertEqual(aggregate_rupees(ExecutionLog.objects.all()), 88)

    def test_an_unpriced_run_falls_back_to_the_blended_rate(self):
        """A model missing from the registry must not make its runs free.

        This is the property that keeps the cap working on exactly the models
        nobody has got around to pricing.
        """
        self._run(cost_source='unpriced', tokens_used=2_000_000)
        self.assertEqual(
            aggregate_rupees(ExecutionLog.objects.all()), rupees_for(2_000_000),
        )

    def test_a_mixed_month_adds_both_halves(self):
        self._run(cost_usd=Decimal('1.00'), cost_source='billed', tokens_used=10)
        self._run(cost_source='unpriced', tokens_used=2_000_000)
        self.assertEqual(
            aggregate_rupees(ExecutionLog.objects.all()),
            88 + rupees_for(2_000_000),
        )

    def test_a_sub_rupee_run_is_never_free(self):
        """The same reason `rupees_for` rounds up: no unbounded free tail."""
        self._run(cost_usd=Decimal('0.000200'), cost_source='estimated')
        self.assertEqual(aggregate_rupees(ExecutionLog.objects.all()), 1)
