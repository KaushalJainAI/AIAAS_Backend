"""
Reading a provider's usage object.

The tests that matter here are the ones about the *convention*: every other
field is a rename, but confusing inclusive and exclusive counting produces a
number that is wrong by the size of the cached prefix and looks entirely
plausible. There is no error to assert on, so the arithmetic is asserted
instead.
"""
from decimal import Decimal

from django.test import SimpleTestCase

from llm.usage import EMPTY_USAGE, TokenUsage, normalize


class InclusiveConventionTests(SimpleTestCase):
    """OpenAI and everything OpenAI-compatible: prompt_tokens INCLUDES cache."""

    def test_cached_tokens_are_subtracted_from_the_prompt(self):
        usage = normalize({
            "prompt_tokens": 1000,
            "completion_tokens": 200,
            "total_tokens": 1200,
            "prompt_tokens_details": {"cached_tokens": 800},
        }, "inclusive")
        # 800 of the 1000 came from cache, so only 200 are billed at full rate.
        self.assertEqual(usage.input, 200)
        self.assertEqual(usage.cached_read, 800)
        self.assertEqual(usage.output, 200)
        # The buckets are disjoint and reconstruct the prompt.
        self.assertEqual(usage.prompt, 1000)
        self.assertEqual(usage.billable_total, 1200)

    def test_a_prompt_that_is_entirely_cached_bills_no_full_rate_input(self):
        usage = normalize({
            "prompt_tokens": 500, "completion_tokens": 10,
            "prompt_tokens_details": {"cached_tokens": 500},
        }, "inclusive")
        self.assertEqual(usage.input, 0)
        self.assertEqual(usage.cached_read, 500)

    def test_details_that_exceed_the_prompt_clamp_rather_than_go_negative(self):
        """A provider whose own numbers disagree must not produce a credit."""
        usage = normalize({
            "prompt_tokens": 100, "completion_tokens": 5,
            "prompt_tokens_details": {"cached_tokens": 400},
        }, "inclusive")
        self.assertEqual(usage.input, 0)


class ExclusiveConventionTests(SimpleTestCase):
    """Anthropic's native shape: input_tokens EXCLUDES cache."""

    def test_cache_is_added_to_the_prompt_not_taken_out_of_it(self):
        usage = normalize({
            "input_tokens": 200,
            "output_tokens": 200,
            "cache_read_input_tokens": 800,
            "cache_creation_input_tokens": 50,
        }, "exclusive")
        self.assertEqual(usage.input, 200)
        self.assertEqual(usage.cached_read, 800)
        self.assertEqual(usage.cached_write, 50)
        self.assertEqual(usage.prompt, 1050)

    def test_the_same_payload_read_with_the_wrong_convention_is_silently_wrong(self):
        """The failure this module exists to prevent, pinned so it stays visible.

        Nothing raises. The only symptom is that 800 cached tokens vanish out
        of the billable prompt, which on a long run is most of the bill.
        """
        raw = {
            "input_tokens": 200, "output_tokens": 200,
            "cache_read_input_tokens": 800,
        }
        right = normalize(raw, "exclusive")
        wrong = normalize(raw, "inclusive")
        self.assertEqual(right.prompt, 1000)
        self.assertEqual(wrong.prompt, 800)
        self.assertEqual(wrong.input, 0)


class ShapeTolerangeTests(SimpleTestCase):
    def test_a_missing_total_is_derived_from_the_parts(self):
        usage = normalize({"prompt_tokens": 30, "completion_tokens": 12})
        self.assertEqual(usage.total, 42)

    def test_a_reported_total_is_kept_rather_than_recomputed(self):
        """It is evidence about whether we read the rest correctly."""
        usage = normalize({
            "prompt_tokens": 30, "completion_tokens": 12, "total_tokens": 99,
        })
        self.assertEqual(usage.total, 99)

    def test_reasoning_tokens_are_recorded_but_not_added_to_output(self):
        usage = normalize({
            "prompt_tokens": 10, "completion_tokens": 100,
            "completion_tokens_details": {"reasoning_tokens": 80},
        })
        self.assertEqual(usage.output, 100)
        self.assertEqual(usage.reasoning, 80)

    def test_openrouter_reports_its_actual_charge(self):
        usage = normalize({"prompt_tokens": 10, "completion_tokens": 5, "cost": 0.0031})
        self.assertEqual(usage.reported_cost_usd, Decimal("0.0031"))

    def test_a_float_cost_does_not_acquire_binary_drift(self):
        usage = normalize({"prompt_tokens": 1, "cost": 0.0000123})
        self.assertEqual(usage.reported_cost_usd, Decimal("0.0000123"))

    def test_junk_degrades_to_empty_rather_than_raising(self):
        for junk in (None, [], "usage", {}, {"prompt_tokens": "many"}):
            self.assertTrue(normalize(junk).is_empty, junk)

    def test_empty_is_distinguishable_from_a_genuine_zero(self):
        """Not reported and nothing consumed must not look alike."""
        self.assertTrue(EMPTY_USAGE.is_empty)
        self.assertFalse(normalize({"prompt_tokens": 1}).is_empty)


class AccumulationTests(SimpleTestCase):
    def test_usages_add_field_wise(self):
        a = TokenUsage(input=10, output=5, cached_read=2, total=17)
        b = TokenUsage(input=3, output=1, cached_write=4, total=8)
        total = a + b
        self.assertEqual((total.input, total.output), (13, 6))
        self.assertEqual((total.cached_read, total.cached_write), (2, 4))
        self.assertEqual(total.total, 25)

    def test_sum_starts_from_zero_without_a_seed(self):
        usages = [TokenUsage(input=i, total=i) for i in range(4)]
        self.assertEqual(sum(usages, EMPTY_USAGE).input, 6)

    def test_a_missing_reported_cost_does_not_count_as_zero(self):
        """Half a bill reported confidently is worse than no bill reported.

        Adding a reported $0.004 to an unreported call as though the second
        were free would understate the total while looking authoritative.
        """
        billed = TokenUsage(input=1, reported_cost_usd=Decimal("0.004"))
        estimated = TokenUsage(input=1)
        self.assertEqual((billed + billed).reported_cost_usd, Decimal("0.008"))
        # One side reported, so the sum still carries what is known — but the
        # `cost_source` machinery in `pricing.combine_sources` is what stops
        # the *total* being presented as billed.
        self.assertEqual((billed + estimated).reported_cost_usd, Decimal("0.004"))
        self.assertIsNone((estimated + estimated).reported_cost_usd)
