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


class CurationCostTests(TestCase):
    """The fold is a model call, and it has to cost money like one.

    Curation exists to keep a long run affordable, and it spends a little to do
    it. That spend reached `total_tokens` from the day curation shipped — the
    comment in `curate_node` says why — but it reached no cost column, because
    a fold is deliberately not an `AgentTurn` and the rollup sums turns. So the
    guardrail curation serves could not see curation's own bill.
    """

    def setUp(self):
        self.user = User.objects.create_user(username='folder', password='pw')
        self.agent = SubAgent.objects.create(user=self.user, name='F')
        self.log = ExecutionLog.objects.create(
            user=self.user, subagent=self.agent, status='running',
            started_at=timezone.now(),
        )
        AgentTurn.objects.create(
            execution=self.log, index=1, decision='answer', model_id='m',
            cost_usd=Decimal('0.010'), cost_source='estimated',
        )

    def _close(self, **kwargs):
        from agents.agent.runtime import _roll_up_cost
        _roll_up_cost(self.log, **kwargs)
        return self.log

    def test_the_folds_cost_is_added_to_the_run(self):
        log = self._close(extra_cost_usd=Decimal('0.002'),
                          extra_cost_source='estimated')
        self.assertEqual(log.cost_usd, Decimal('0.012'))

    def test_a_run_that_never_curated_is_unaffected(self):
        self.assertEqual(self._close().cost_usd, Decimal('0.010'))

    def test_an_unpriced_fold_makes_the_run_unpriced(self):
        """The fold model is often a different one, and may not be in the table."""
        log = self._close(extra_cost_usd=Decimal('0'),
                          extra_cost_source='unpriced')
        self.assertEqual(log.cost_source, 'unpriced')

    def test_the_stream_accumulates_folds_across_passes(self):
        """A long run curates several times; each fold is its own call."""
        from unittest.mock import AsyncMock

        from agents.agent.stream import AgentRunStream

        stream = AgentRunStream(self.log, broadcaster=AsyncMock())
        for _ in range(3):
            async_to_sync(stream.on_curation)(
                results_compacted=1, steps_folded=2, tokens_before=100,
                tokens_after=50, summary_tokens=30,
                summary_cost_usd=Decimal('0.001'),
                summary_cost_source='estimated',
            )
        self.assertEqual(stream.curation['cost_usd'], Decimal('0.003'))
        self.assertEqual(stream.curation['cost_source'], 'estimated')

    def test_a_fold_that_reported_no_cost_does_not_disturb_the_source(self):
        from unittest.mock import AsyncMock

        from agents.agent.stream import AgentRunStream

        stream = AgentRunStream(self.log, broadcaster=AsyncMock())
        async_to_sync(stream.on_curation)(
            results_compacted=1, steps_folded=0, tokens_before=100,
            tokens_after=50, summary_tokens=0,
        )
        self.assertEqual(stream.curation['cost_usd'], Decimal('0'))
        self.assertEqual(stream.curation['cost_source'], '')


class InsightsSpendTests(TestCase):
    """`cost_breakdown` must count chat, and label every sum honestly."""

    def setUp(self):
        from chat.models import ChatSession

        self.user = User.objects.create_user(username='insight', password='pw')
        self.agent = SubAgent.objects.create(user=self.user, name='Worker')
        self.session = ChatSession.objects.create(user=self.user, title='C')

    def _run(self, **kwargs):
        return ExecutionLog.objects.create(
            user=self.user, subagent=self.agent, status='completed', **kwargs)

    def _answer(self, cost, source, paid_by=''):
        from chat.models import ChatMessage

        return ChatMessage.objects.create(
            session=self.session, role='assistant', content='a',
            cost_usd=Decimal(cost), cost_source=source, paid_by=paid_by,
            input_tokens=100, output_tokens=10,
        )

    def _breakdown(self):
        from logs.queries import cost_breakdown

        return cost_breakdown(self.user, days=30)

    def test_chat_spend_is_counted(self):
        """It was left out entirely: chat has no ExecutionLog to sum."""
        self._answer('0.010', 'billed', 'own_key')
        self._answer('0.002', 'billed', 'platform')
        chat = self._breakdown()['chat']
        self.assertEqual(chat['messages'], 2)
        self.assertEqual(chat['cost_usd'], '0.012000')
        self.assertEqual(chat['cost_source'], 'billed')
        self.assertEqual(chat['paid_by'], {'platform': 1, 'own_key': 1})

    def test_the_all_up_total_adds_both_and_takes_the_weaker_label(self):
        self._run(cost_usd=Decimal('1.00'), cost_source='estimated')
        self._answer('0.50', 'billed')
        data = self._breakdown()
        self.assertEqual(data['all_cost_usd'], '1.500000')
        self.assertEqual(data['all_cost_source'], 'estimated')
        # The agent-only total keeps its old meaning.
        self.assertEqual(data['total_cost_usd'], '1.000000')

    def test_an_agent_billed_throughout_is_labelled_billed(self):
        """Every priced agent used to read `estimated`, billed or not."""
        self._run(cost_usd=Decimal('0.1'), cost_source='billed')
        self._run(cost_usd=Decimal('0.2'), cost_source='billed')
        row = self._breakdown()['by_workflow'][0]
        self.assertEqual(row['cost_source'], 'billed')

    def test_one_unpriced_run_makes_the_agent_unpriced(self):
        self._run(cost_usd=Decimal('0.1'), cost_source='billed')
        self._run(cost_source='unpriced', tokens_used=5)
        self.assertEqual(self._breakdown()['by_workflow'][0]['cost_source'],
                         'unpriced')

    def test_a_deleted_agents_runs_are_grouped_and_named(self):
        self._run(cost_usd=Decimal('0.1'), cost_source='estimated')
        self.agent.delete()
        row = self._breakdown()['by_workflow'][0]
        self.assertIsNone(row['workflow_id'])
        self.assertEqual(row['workflow_name'], 'Deleted agents')

    def test_nothing_spent_is_not_a_confident_zero(self):
        data = self._breakdown()
        self.assertEqual(data['chat']['cost_source'], 'unpriced')
        self.assertEqual(data['all_cost_source'], 'unpriced')
