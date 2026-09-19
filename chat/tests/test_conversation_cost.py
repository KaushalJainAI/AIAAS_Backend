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


class FollowUpCostTests(TestCase):
    """The follow-up questions are a model call on the user's model, and cost.

    They were generated after every answer and their usage was discarded, so
    the header's conversation cost left out one call per turn while looking
    exact.
    """

    def setUp(self):
        provider = AIProvider.objects.create(name='OpenRouter', slug='openrouter')
        AIModel.objects.create(
            provider=provider, name='Test', value='vendor/model',
            input_price_per_million=Decimal('2.0000'),
            output_price_per_million=Decimal('12.0000'),
        )
        self.user = User.objects.create_user(username='asker', password='pw')
        self.session = ChatSession.objects.create(
            user=self.user, title='Chat', llm_model='vendor/model',
        )

    def _persist(self, follow_usage):
        from unittest.mock import patch

        from chat.turn.agent import TurnContext, TurnResult
        from chat.turn.pipeline import _persist_answer

        async def follow_ups(*_a, usage_sink=None, **_k):
            if usage_sink is not None and follow_usage is not None:
                usage_sink.append(follow_usage)
            return ['a?']

        turn = TurnContext(
            provider='openrouter', model='vendor/model', system_message='s',
            user_id=self.user.id, session_id=str(self.session.id),
            intent='chat', user_text='q',
        )
        answer_usage = TokenUsage(input=1_000_000, total=1_000_000,
                                  reported_cost_usd=Decimal('2'))
        result = TurnResult(
            answer='A considered answer. ' * 50, metadata={}, tool_trace=[],
            usage=answer_usage, tokens=1_000_000,
        )
        with patch('chat.turn.agent.suggest_follow_ups', follow_ups):
            message = async_to_sync(_persist_answer)(
                session=self.session, user=self.user, turn=turn,
                result=result, question='q', intent='chat', elapsed_s=1,
            )
        self.session.refresh_from_db()
        return message

    def test_the_follow_up_call_is_added_to_the_turn(self):
        message = self._persist(TokenUsage(
            input=100_000, total=100_000, reported_cost_usd=Decimal('0.5'),
        ))
        self.assertEqual(message.cost_usd, Decimal('2.500000'))
        self.assertEqual(message.cost_source, 'billed')
        self.assertEqual(self.session.total_cost_usd, Decimal('2.500000'))
        self.assertEqual(self.session.total_tokens_used, 1_100_000)

    def test_an_estimated_follow_up_stops_the_turn_claiming_billed(self):
        # 0.1M input at $2/M, from the price table.
        message = self._persist(TokenUsage(input=100_000, total=100_000))
        self.assertEqual(message.cost_usd, Decimal('2.200000'))
        self.assertEqual(message.cost_source, 'estimated')

    def test_no_follow_up_call_changes_nothing(self):
        message = self._persist(None)
        self.assertEqual(message.cost_usd, Decimal('2.000000'))
        self.assertEqual(message.cost_source, 'billed')
        self.assertEqual(self.session.total_tokens_used, 1_000_000)


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


class PayerTests(TestCase):
    """Whose money the chip's figure is.

    A bare `₹1.02` was read as "this chat charged me ₹1.02" — true only on the
    user's own key. On the platform's key the user pays credits instead.
    """

    def _payer(self, *, own=None, platform=None, free=False, provider='openrouter'):
        from unittest.mock import AsyncMock, patch

        from llm import access

        with patch.object(access, '_resolve_credential', AsyncMock(return_value=own)), \
             patch.object(access, '_platform_api_key', lambda _p: platform), \
             patch.object(access.credits, 'is_free_model', AsyncMock(return_value=free)):
            return async_to_sync(access.payer)(provider=provider, model='m', user_id=1)

    def test_the_users_own_key_wins_over_the_platforms(self):
        """The same order `_build_request` resolves keys in."""
        self.assertEqual(self._payer(own=7, platform='pk'), 'own_key')

    def test_the_platform_key_on_a_paid_model_is_credits(self):
        self.assertEqual(self._payer(platform='pk'), 'platform')

    def test_the_platform_key_on_a_free_model_costs_nobody(self):
        self.assertEqual(self._payer(platform='pk', free=True), 'free')

    def test_a_keyless_provider_is_local(self):
        self.assertEqual(self._payer(provider='ollama'), 'local')

    def test_no_key_at_all_is_unknown_rather_than_a_guess(self):
        self.assertEqual(self._payer(), '')

    def test_a_conversation_that_switched_keys_reads_mixed_for_good(self):
        from chat.turn.pipeline import _combine_payers

        self.assertEqual(_combine_payers('', 'platform'), 'platform')
        self.assertEqual(_combine_payers('platform', 'platform'), 'platform')
        self.assertEqual(_combine_payers('platform', 'own_key'), 'mixed')
        self.assertEqual(_combine_payers('mixed', 'platform'), 'mixed')
        # An unknown turn leaves what is known alone.
        self.assertEqual(_combine_payers('own_key', ''), 'own_key')


class SideCallCostTests(FollowUpCostTests):
    """A model call inside a tool, on another model, is part of the turn too.

    The vision witness answers `ask_vision` on its own model; its usage never
    reached the graph state the turn is priced from.
    """

    def setUp(self):
        super().setUp()
        AIModel.objects.create(
            provider=AIProvider.objects.get(slug='openrouter'), name='Witness',
            value='vendor/witness',
            input_price_per_million=Decimal('1.0000'),
            output_price_per_million=Decimal('1.0000'),
        )

    def test_a_witness_call_is_priced_on_its_own_model(self):
        from chat.turn import side_calls

        side_calls.start()
        side_calls.record('vendor/witness', TokenUsage(input=1_000_000,
                                                       total=1_000_000))
        message = self._persist(None)
        # $2 billed for the answer + $1 estimated for the witness.
        self.assertEqual(message.cost_usd, Decimal('3.000000'))
        self.assertEqual(message.cost_source, 'estimated')
        self.assertEqual(self.session.total_tokens_used, 2_000_000)

    def test_recording_outside_a_turn_is_a_no_op(self):
        from contextvars import Context

        from chat.turn import side_calls

        def outside():
            side_calls.record('vendor/witness', TokenUsage(input=5, total=5))
            return side_calls.collected()

        self.assertEqual(Context().run(outside), [])

    # Inherited follow-up cases are not re-run here.
    test_the_follow_up_call_is_added_to_the_turn = None
    test_an_estimated_follow_up_stops_the_turn_claiming_billed = None
    test_no_follow_up_call_changes_nothing = None
