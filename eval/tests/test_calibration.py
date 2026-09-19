"""Judge calibration: stubbed judge produces the right rates; errors fail closed."""
from unittest.mock import patch

from asgiref.sync import async_to_sync
from django.contrib.auth.models import User
from django.test import TestCase

from eval import calibration
from eval.benchmarks.calibration.judge_set import ROWS


class CalibrationTests(TestCase):
    def test_set_has_30_rows_with_10_subtle(self):
        self.assertGreaterEqual(len(ROWS), 30)
        self.assertGreaterEqual(sum(1 for r in ROWS if r['kind'] == 'subtle'), 10)

    def test_stubbed_judge_produces_rates(self):
        from llm.access import Completion
        from llm.usage import TokenUsage

        async def fake_complete(**kwargs):
            prompt = kwargs.get('prompt', '')
            # Pass when the rubric's key phrase appears in the answer section.
            good = 'Paris' in prompt and 'ANSWER TO GRADE:\nThe capital is Paris' in prompt
            content = '{"score": %s, "reason": "stub"}' % (0.9 if good else 0.1)
            return Completion(content=content, usage=TokenUsage(input=10, output=5, total=15),
                              tokens=15)

        rows = [
            {'id': 'a', 'goal': 'g', 'rubric': 'Says Paris.',
             'answer': 'The capital is Paris.', 'tool_trace': [], 'label': True, 'kind': 'good'},
            {'id': 'b', 'goal': 'g', 'rubric': 'Says Paris.',
             'answer': 'Berlin.', 'tool_trace': [], 'label': False, 'kind': 'bad'},
        ]
        with patch('llm.access.complete', fake_complete):
            result = async_to_sync(calibration.calibrate)(rows, user_id=1)
        self.assertEqual(result['n'], 2)
        # Both correct: agreement 100%, no false passes/fails.
        self.assertAlmostEqual(result['agreement'], 1.0)
        self.assertAlmostEqual(result['false_pass_rate'], 0.0)

    def test_judge_error_is_a_failed_grade_reported_separately(self):
        async def boom(**kwargs):
            raise RuntimeError('provider down')

        rows = [{'id': 'a', 'goal': 'g', 'rubric': 'r',
                 'answer': 'x', 'tool_trace': [], 'label': False, 'kind': 'bad'}]
        with patch('llm.access.complete', boom):
            result = async_to_sync(calibration.calibrate)(rows, user_id=1)
        self.assertEqual(result['errors'], 1)
        # Fail closed: an errored judge counts as failed, and here the label
        # was False — so it still "agrees", but the error is reported apart.
        self.assertEqual(len(result['details']), 1)
