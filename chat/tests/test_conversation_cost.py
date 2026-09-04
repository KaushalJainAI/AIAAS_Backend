"""
What a conversation cost.

Chat has no `ExecutionLog`, so none of the agent-side cost machinery reaches it:
the money has to be recorded on the messages and summed onto the session. These
tests cover the two places that can go wrong — the per-message write, and the
running total that must not silently drop a turn it could not price.
"""
from decimal import Decimal

from asgiref.sync import async_to_sync
from django.contrib.auth.models import User
from django.test import TestCase

from chat.models import ChatMessage, ChatSession
from llm.models import AIModel, AIProvider
from llm.usage import TokenUsage, normalize


class TurnPricingTests(TestCase):
    """`_price_turn` is the one place a chat turn becomes money."""

    def setUp(self):
        provider = AIProvider.objects.create(name='OpenRouter', slug='openrouter')
        AIModel.objects.create(
            provider=provider, name='Test', value='vendor/model',
            input_price_per_million=Decimal('2.0000'),
            output_price_per_million=Decimal('12.0000'),
            cached_input_price_per_million=Decimal('0.2000'),
        )

    def _price(self, model_id, usage):
        from chat.turn.pipeline import _price_turn

        return async_to_sync(_price_turn)(model_id, usage)

    def test_a_turn_is_priced_from_its_breakdown_not_its_total(self):
        cost, source = self._price('vendor/model', normalize({
            'prompt_tokens': 1_000_000, 'completion_tokens': 100_000,
            'prompt_tokens_details': {'cached_tokens': 800_000},
        }))
        # 0.2M*$2 + 0.8M*$0.20 + 0.1M*$12
        self.assertEqual(cost, Decimal('1.760000'))
        self.assertEqual(source, 'estimated')

    def test_a_provider_reported_cost_wins(self):
        cost, source = self._price('vendor/model', normalize({
            'prompt_tokens': 100, 'completion_tokens': 10, 'cost': 0.004,
        }))
        self.assertEqual(source, 'billed')
        self.assertEqual(cost, Decimal('0.004000'))

    def test_an_unknown_model_is_unpriced_rather_than_free(self):
        cost, source = self._price('vendor/nobody-knows', normalize({
            'prompt_tokens': 100, 'completion_tokens': 10,
        }))
        self.assertEqual(source, 'unpriced')
        self.assertEqual(cost, Decimal('0'))


class SessionTotalTests(TestCase):
    """The conversation's running total, and what it does with a gap."""

    def setUp(self):
        self.user = User.objects.create_user(username='talker', password='pw')
        self.session = ChatSession.objects.create(user=self.user, title='Chat')

    def _turn(self, cost, source, tokens=100):
        """The accumulation `_persist_answer` performs, in isolation."""
        from llm.pricing import combine_sources

        ChatMessage.objects.create(
            session=self.session, role='assistant', content='hi',
            model_id='vendor/model', cost_usd=Decimal(cost), cost_source=source,
            input_tokens=tokens,
        )
        self.session.total_tokens_used += tokens
        self.session.total_cost_usd += Decimal(cost)
        self.session.cost_source = combine_sources(
            [self.session.cost_source, source]
        )
        self.session.save()

    def test_a_new_conversation_starts_unpriced_not_free(self):
        """Nothing has been spent and nothing has been priced."""
        self.assertEqual(self.session.total_cost_usd, Decimal('0'))
        self.assertEqual(self.session.cost_source, '')

    def test_turns_accumulate(self):
        self._turn('0.004', 'estimated')
        self._turn('0.006', 'estimated')
        self.session.refresh_from_db()
        self.assertEqual(self.session.total_cost_usd, Decimal('0.010000'))
        self.assertEqual(self.session.cost_source, 'estimated')

    def test_a_conversation_billed_throughout_stays_billed(self):
        self._turn('0.004', 'billed')
        self._turn('0.001', 'billed')
        self.session.refresh_from_db()
        self.assertEqual(self.session.cost_source, 'billed')

    def test_one_unpriced_turn_makes_the_conversation_unpriced_for_good(self):
        """And stays that way once a later turn can be priced again.

        From the moment a turn could not be priced, the total is missing money
        nobody can put back — so it must never go back to claiming precision.
        """
        self._turn('0.004', 'billed')
        self._turn('0', 'unpriced')
        self._turn('0.004', 'billed')
        self.session.refresh_from_db()
        self.assertEqual(self.session.cost_source, 'unpriced')

    def test_a_message_carries_the_model_that_produced_it(self):
        """Not the session's current model, which the user may have changed."""
        self._turn('0.004', 'estimated')
        self.session.llm_model = 'vendor/something-else'
        self.session.save()
        message = ChatMessage.objects.get(session=self.session)
        self.assertEqual(message.model_id, 'vendor/model')


class TurnResultUsageTests(TestCase):
    """The breakdown has to survive the graph, or none of the above can work."""

    def test_the_graph_state_accumulates_usage_alongside_the_total(self):
        from llm.usage import EMPTY_USAGE

        first = TokenUsage(input=100, output=20, cached_read=50, total=170)
        second = TokenUsage(input=200, output=30, total=230)
        total = EMPTY_USAGE + first + second
        self.assertEqual(total.input, 300)
        self.assertEqual(total.output, 50)
        self.assertEqual(total.cached_read, 50)
        self.assertEqual(total.total, 400)

    def test_a_turn_result_defaults_to_empty_usage_not_none(self):
        """So every caller can add to it without a None check."""
        from chat.turn.agent import TurnResult

        self.assertTrue(TurnResult().usage.is_empty)
