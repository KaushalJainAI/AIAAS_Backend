"""
Grader tests.

The failure this app has to avoid above all others is a suite that reports a
high score while measuring nothing, so the cases here concentrate on the ways
that happens: a grader that validates but cannot run, a judge that fails open,
and a case with nothing to assert.
"""
from unittest.mock import patch

from asgiref.sync import async_to_sync
from django.test import SimpleTestCase

from eval import graders
from eval.graders import GradeContext, GraderError


def grade(specs, **ctx):
    return async_to_sync(graders.grade_all)(specs, GradeContext(**ctx))


class RegistryTests(SimpleTestCase):
    def test_catalog_is_the_registry(self):
        # The picker and the runner must not be able to disagree about what a
        # grader is — that is the whole point of one registry.
        self.assertEqual(
            {entry['type'] for entry in graders.catalog()},
            set(graders.REGISTRY),
        )

    def test_only_the_judge_calls_a_model(self):
        # Everything else has to be runnable with no provider, no credential
        # and no network, or a sweep's grading cost becomes unpredictable.
        calling = {g.name for g in graders.REGISTRY.values() if g.calls_model}
        self.assertEqual(calling, {'llm_judge'})


class ValidationTests(SimpleTestCase):
    def test_unknown_grader_is_refused(self):
        with self.assertRaises(GraderError):
            graders.validate_spec({'type': 'vibes'})

    def test_missing_required_param_is_refused(self):
        with self.assertRaises(GraderError):
            graders.validate_spec({'type': 'contains'})

    def test_unknown_param_is_refused(self):
        # A typo'd key that was silently ignored would be a grader asserting
        # something other than what its author wrote.
        with self.assertRaises(GraderError):
            graders.validate_spec({'type': 'contains', 'value': 'x', 'valeu': 'y'})

    def test_weight_must_be_positive(self):
        with self.assertRaises(GraderError):
            graders.validate_spec({'type': 'no_error', 'weight': 0})

    def test_valid_spec_comes_back_normalised(self):
        spec = graders.validate_spec({'type': 'contains', 'value': 'hi'})
        self.assertEqual(spec['weight'], 1.0)

    def test_specs_must_be_a_list(self):
        with self.assertRaises(GraderError):
            graders.validate_specs({'type': 'contains', 'value': 'hi'})


class TextGraderTests(SimpleTestCase):
    def test_contains_ignores_case_by_default(self):
        _, _, passed = grade([{'type': 'contains', 'value': 'PARIS'}],
                             answer='The capital is paris.')
        self.assertTrue(passed)

    def test_contains_can_be_case_sensitive(self):
        _, _, passed = grade(
            [{'type': 'contains', 'value': 'PARIS', 'ignore_case': False}],
            answer='The capital is paris.',
        )
        self.assertFalse(passed)

    def test_not_contains(self):
        _, _, passed = grade([{'type': 'not_contains', 'value': 'as an ai'}],
                             answer='As an AI language model, I cannot help.')
        self.assertFalse(passed)

    def test_equals_strips_and_folds(self):
        _, _, passed = grade([{'type': 'equals', 'value': 'yes'}], answer='  YES \n')
        self.assertTrue(passed)

    def test_regex(self):
        _, _, passed = grade([{'type': 'regex', 'pattern': r'\d{4}-\d{2}-\d{2}'}],
                             answer='due 2026-08-24')
        self.assertTrue(passed)

    def test_invalid_regex_fails_the_case_and_says_why(self):
        # A broken pattern is a broken case, not a failing agent, and the
        # detail has to say so or the run reads as a regression.
        grades, _, passed = grade([{'type': 'regex', 'pattern': '('}], answer='x')
        self.assertFalse(passed)
        self.assertIn('invalid pattern', grades[0].detail)

    def test_length_bounds(self):
        _, _, short = grade([{'type': 'min_length', 'value': 10}], answer='hi')
        _, _, long = grade([{'type': 'max_length', 'value': 3}], answer='hello')
        self.assertFalse(short)
        self.assertFalse(long)


class StructureAndBehaviourTests(SimpleTestCase):
    def test_json_key_requires_structured_output(self):
        grades, _, passed = grade([{'type': 'json_key', 'key': 'sources'}], answer='prose')
        self.assertFalse(passed)
        self.assertIn('structured', grades[0].detail)

    def test_json_key_can_assert_a_value(self):
        _, _, passed = grade(
            [{'type': 'json_key', 'key': 'status', 'equals': 'ok'}],
            structured={'status': 'ok'},
        )
        self.assertTrue(passed)

    def test_contract_failure_is_reported_verbatim(self):
        grades, _, passed = grade([{'type': 'contract'}],
                                  contract_error='missing key: text')
        self.assertFalse(passed)
        self.assertEqual(grades[0].detail, 'missing key: text')

    def test_tool_used_reads_the_trace(self):
        trace = [{'tool': 'web_search'}, {'name': 'read_url'}]
        _, _, used = grade([{'type': 'tool_used', 'tool': 'read_url'}], tool_trace=trace)
        _, _, avoided = grade([{'type': 'tool_not_used', 'tool': 'web_search'}],
                              tool_trace=trace)
        self.assertTrue(used)
        self.assertFalse(avoided)

    def test_budget_graders(self):
        _, _, tokens_ok = grade([{'type': 'max_tokens', 'value': 100}], tokens=250)
        _, _, time_ok = grade([{'type': 'max_duration_ms', 'value': 5000}], duration_ms=900)
        self.assertFalse(tokens_ok)
        self.assertTrue(time_ok)


class FoldingTests(SimpleTestCase):
    def test_a_case_passes_only_when_every_grader_passes(self):
        _, _, passed = grade([
            {'type': 'contains', 'value': 'paris'},
            {'type': 'not_contains', 'value': 'paris'},
        ], answer='paris')
        self.assertFalse(passed)

    def test_score_is_weighted(self):
        _, score, _ = grade([
            {'type': 'contains', 'value': 'paris', 'weight': 3},
            {'type': 'contains', 'value': 'berlin', 'weight': 1},
        ], answer='paris')
        self.assertAlmostEqual(score, 0.75)

    def test_no_graders_means_undecided_not_passed(self):
        # Vacuous truth is how an empty suite reports 100%.
        grades, score, passed = grade([])
        self.assertEqual(grades, [])
        self.assertEqual(score, 0.0)
        self.assertIsNone(passed)

    def test_a_grader_that_raises_fails_closed(self):
        with patch.dict(graders.REGISTRY, {}, clear=False):
            broken = graders.Grader('boom', (), (), lambda s, c: 1 / 0)
            graders.REGISTRY['boom'] = broken
            try:
                grades, _, passed = grade([{'type': 'boom'}])
            finally:
                graders.REGISTRY.pop('boom')
        self.assertFalse(passed)
        self.assertIn('grader raised', grades[0].detail)

    def test_a_removed_grader_fails_closed(self):
        grades, _, passed = grade([{'type': 'retired_grader'}])
        self.assertFalse(passed)
        self.assertIn('no longer exists', grades[0].detail)


class JudgeTests(SimpleTestCase):
    def test_reply_parsing_tolerates_fences_and_prose(self):
        score, reason = graders._judge_reply(
            'Sure!\n```json\n{"score": 0.4, "reason": "vague"}\n```'
        )
        self.assertAlmostEqual(score, 0.4)
        self.assertEqual(reason, 'vague')

    def test_reply_without_a_score_is_an_error_not_a_pass(self):
        with self.assertRaises(ValueError):
            graders._judge_reply('Looks good to me!')

    def test_score_is_clamped(self):
        self.assertEqual(graders._judge_reply('{"score": 7}')[0], 1.0)

    def test_a_judge_that_cannot_run_fails_closed(self):
        # A judge that passes everything when the provider is down would turn
        # an outage into a green suite.
        async def boom(**kwargs):
            raise RuntimeError('provider down')

        with patch('llm.access.complete', boom):
            grades, _, passed = grade(
                [{'type': 'llm_judge', 'rubric': 'names the capital'}],
                answer='Paris', user_id=1,
            )
        self.assertFalse(passed)
        self.assertIn('judge unavailable', grades[0].detail)

    def test_no_rubric_is_reported_rather_than_guessed(self):
        grades, _, passed = grade([{'type': 'llm_judge'}], answer='Paris', user_id=1)
        self.assertFalse(passed)
        self.assertIn('no rubric', grades[0].detail)

    def test_threshold_decides_the_verdict(self):
        class Reply:
            content = '{"score": 0.8, "reason": "good"}'

        async def fake(**kwargs):
            return Reply()

        with patch('llm.access.complete', fake):
            _, _, strict = grade(
                [{'type': 'llm_judge', 'rubric': 'r', 'threshold': 0.9}], user_id=1,
            )
            _, score, lenient = grade(
                [{'type': 'llm_judge', 'rubric': 'r', 'threshold': 0.7}], user_id=1,
            )
        self.assertFalse(strict)
        self.assertTrue(lenient)
        self.assertAlmostEqual(score, 0.8)
