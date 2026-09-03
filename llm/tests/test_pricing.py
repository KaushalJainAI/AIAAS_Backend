"""
Turning usage into money.

Two properties carry most of the weight here: output and cache are priced at
their own rates rather than at the input rate (the reason a single token total
cannot be costed at all), and `unpriced` never masquerades as free.
"""
from decimal import Decimal

from django.test import SimpleTestCase, TestCase

from llm.models import AIModel, AIProvider
from llm.pricing import (
    combine_sources,
    cost_for_usage,
    estimate_cost_usd,
    format_usd,
    quantize_usd,
)
from llm.usage import EMPTY_USAGE, TokenUsage


class RateApplicationTests(SimpleTestCase):
    def test_each_bucket_is_priced_at_its_own_rate(self):
        cost = estimate_cost_usd(
            input_tokens=1_000_000,
            output_tokens=1_000_000,
            cached_read_tokens=1_000_000,
            cached_write_tokens=1_000_000,
            input_price_per_million="2.00",
            output_price_per_million="12.00",
            cached_input_price_per_million="0.20",
            cache_write_price_per_million="2.50",
        )
        self.assertEqual(cost, Decimal("16.700000"))

    def test_costing_a_flat_total_at_the_input_rate_understates_it_sixfold(self):
        """Why `tokens_used` alone could never be costed.

        The same 1.2M tokens costs $2.40 if you pretend they are all input and
        $4.40 priced properly — and the gap widens with the output share.
        """
        naive = estimate_cost_usd(
            input_tokens=1_200_000, input_price_per_million="2.00",
        )
        real = estimate_cost_usd(
            input_tokens=1_000_000, output_tokens=200_000,
            input_price_per_million="2.00", output_price_per_million="12.00",
        )
        self.assertEqual(naive, Decimal("2.400000"))
        self.assertEqual(real, Decimal("4.400000"))

    def test_a_missing_cache_read_rate_falls_back_to_the_input_rate(self):
        """No stated discount means no discount, never free."""
        cost = estimate_cost_usd(
            cached_read_tokens=1_000_000, input_price_per_million="3.00",
        )
        self.assertEqual(cost, Decimal("3.000000"))

    def test_a_missing_cache_write_rate_is_free(self):
        """OpenAI does not charge to write, and most models have no cache."""
        cost = estimate_cost_usd(
            cached_write_tokens=1_000_000, input_price_per_million="3.00",
        )
        self.assertEqual(cost, Decimal("0.000000"))

    def test_a_cheap_turn_does_not_round_away_to_zero(self):
        cost = estimate_cost_usd(
            input_tokens=150, output_tokens=20,
            input_price_per_million="0.15", output_price_per_million="0.60",
        )
        self.assertGreater(cost, Decimal("0"))


class MoneyFormattingTests(SimpleTestCase):
    def test_a_database_sum_is_trimmed_to_six_places(self):
        """SQLite returns a Decimal with excess scale from `Sum`."""
        self.assertEqual(format_usd(Decimal("0.00420000000000000")), "0.004200")

    def test_none_formats_as_a_zero_rather_than_as_none(self):
        self.assertEqual(format_usd(None), "0.000000")
        self.assertEqual(quantize_usd(None), Decimal("0.000000"))


class CostSourceTests(TestCase):
    def setUp(self):
        self.provider = AIProvider.objects.create(name='OpenRouter', slug='openrouter')
        self.local = AIProvider.objects.create(name='Ollama', slug='ollama')
        self.usage = TokenUsage(input=1_000_000, output=1_000_000, total=2_000_000)

    def _model(self, value, **kwargs):
        kwargs.setdefault('provider', self.provider)
        kwargs.setdefault('name', value)
        return AIModel.objects.create(value=value, **kwargs)

    def test_a_provider_reported_cost_wins_over_the_price_table(self):
        """No table we maintain beats the invoice."""
        self._model('priced', input_price_per_million=Decimal('2'),
                    output_price_per_million=Decimal('12'))
        usage = TokenUsage(input=1_000_000, output=1_000_000,
                           reported_cost_usd=Decimal('0.99'))
        cost, source = cost_for_usage('priced', usage)
        self.assertEqual(source, 'billed')
        self.assertEqual(cost, Decimal('0.990000'))

    def test_a_priced_model_estimates(self):
        self._model('priced', input_price_per_million=Decimal('2'),
                    output_price_per_million=Decimal('12'))
        cost, source = cost_for_usage('priced', self.usage)
        self.assertEqual(source, 'estimated')
        self.assertEqual(cost, Decimal('14.000000'))

    def test_an_unknown_model_is_unpriced_not_free(self):
        cost, source = cost_for_usage('nobody/has-heard-of-this', self.usage)
        self.assertEqual(source, 'unpriced')
        self.assertEqual(cost, Decimal('0'))

    def test_a_local_model_is_genuinely_free(self):
        self._model('qwen3:8b', provider=self.local, is_free=True)
        cost, source = cost_for_usage('qwen3:8b', self.usage)
        self.assertEqual(source, 'estimated')
        self.assertEqual(cost, Decimal('0'))

    def test_a_router_with_no_rate_is_unpriced(self):
        """Its price depends on whatever it routed to, which we never see."""
        self._model('openrouter/auto')
        _cost, source = cost_for_usage('openrouter/auto', self.usage)
        self.assertEqual(source, 'unpriced')

    def test_a_retired_model_is_still_priced(self):
        """`is_active` is about what may be offered, not what may be costed.

        A run on a since-deactivated model still cost real money at a rate we
        still hold. Treating it as unpriced would make an agent's spend history
        shrink every time a model was retired.
        """
        self._model('retired', is_active=False,
                    input_price_per_million=Decimal('2'),
                    output_price_per_million=Decimal('12'))
        cost, source = cost_for_usage('retired', self.usage)
        self.assertEqual(source, 'estimated')
        self.assertEqual(cost, Decimal('14.000000'))

    def test_usage_that_was_never_reported_is_unpriced(self):
        self._model('priced', input_price_per_million=Decimal('2'))
        _cost, source = cost_for_usage('priced', EMPTY_USAGE)
        self.assertEqual(source, 'unpriced')


class CombineSourcesTests(SimpleTestCase):
    def test_one_unpriced_turn_makes_the_whole_run_unpriced(self):
        """A confident total that silently omits a turn is the worse answer."""
        self.assertEqual(
            combine_sources(['billed', 'estimated', 'unpriced']), 'unpriced',
        )

    def test_billed_survives_only_when_everything_was_billed(self):
        self.assertEqual(combine_sources(['billed', 'billed']), 'billed')
        self.assertEqual(combine_sources(['billed', 'estimated']), 'estimated')

    def test_nothing_at_all_is_unpriced(self):
        self.assertEqual(combine_sources([]), 'unpriced')
        self.assertEqual(combine_sources(['']), 'unpriced')
