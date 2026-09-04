"""
Charts as data, and the two caps that are not about cost.

`render_html_artifact` could already draw a chart, which was the problem: it
asked a language model to be a rendering engine, inside a frame with no network
so no chart library could load. Every chart looked like a different product.
`render_chart` moves the drawing to a component and leaves the model with the
part it is actually good at — the numbers and what they mean.

Most of what is pinned below is refusal behaviour, because the interesting
decision is that this tool **refuses rather than truncates**. A silently dropped
series is a chart that is quietly wrong about its own subject, and unlike a web
page arriving from outside, the author here is a model that can be told to fold
the tail into "Other".
"""
from __future__ import annotations

import json

from asgiref.sync import async_to_sync
from django.test import SimpleTestCase

from chat.tools.charts import render_chart
from workflow_backend.thresholds import (
    CHART_MAX_POINTS_PER_SERIES,
    CHART_MAX_SERIES,
    CHART_MAX_SERIES_ALL_PAIRS,
)


def call(**args):
    return json.loads(async_to_sync(render_chart)(args, {}))


def series(name, *values):
    return {'name': name, 'points': [{'x': f'p{i}', 'y': v}
                                     for i, v in enumerate(values)]}


class SpecTests(SimpleTestCase):
    def test_a_valid_spec_comes_back_as_a_chart(self):
        out = call(kind='column', title='Revenue by month',
                   series=[series('2026', 10, 20, 30)], y_label='USD')
        self.assertEqual(out['type'], 'chart')
        self.assertEqual(out['kind'], 'column')
        self.assertEqual(out['y_label'], 'USD')
        self.assertEqual(len(out['series'][0]['points']), 3)

    def test_the_model_is_told_not_to_narrate_the_chart(self):
        """Otherwise it reads the numbers back point by point in prose, and the
        chart becomes a duplicate of the paragraph beside it."""
        out = call(kind='line', title='t', series=[series('a', 1, 2)])
        self.assertIn('Do not describe it point by point', out['rendered'])

    def test_an_unknown_kind_is_refused_with_the_list(self):
        out = call(kind='sankey', title='t', series=[series('a', 1)])
        self.assertIn('error', out)
        self.assertIn('column', out['error'])

    def test_a_missing_title_is_refused(self):
        # The title is what the chart is *for*; an untitled chart makes the
        # reader work out the subject from the axes.
        out = call(kind='bar', title='  ', series=[series('a', 1)])
        self.assertIn('error', out)

    def test_points_may_be_pairs_as_well_as_objects(self):
        out = call(kind='line', title='t',
                   series=[{'name': 'a', 'points': [['Jan', 1], ['Feb', 2]]}])
        self.assertEqual(out['series'][0]['points'][0], {'x': 'Jan', 'y': 1.0})


class MissingValueTests(SimpleTestCase):
    """A gap and a zero are different facts and must not be merged."""

    def test_a_non_numeric_y_becomes_a_gap_not_a_zero(self):
        out = call(kind='line', title='t',
                   series=[{'name': 'a', 'points': [
                       {'x': 'Jan', 'y': 5}, {'x': 'Feb', 'y': None},
                       {'x': 'Mar', 'y': 7}]}])
        ys = [p['y'] for p in out['series'][0]['points']]
        self.assertEqual(ys, [5.0, None, 7.0])

    def test_nan_and_infinity_are_gaps(self):
        # Neither has a position on a scale; drawing them as zero would put a
        # dip in the line where no measurement exists.
        out = call(kind='line', title='t',
                   series=[{'name': 'a', 'points': [
                       {'x': 'a', 'y': float('nan')},
                       {'x': 'b', 'y': float('inf')},
                       {'x': 'c', 'y': 3}]}])
        self.assertEqual([p['y'] for p in out['series'][0]['points']],
                         [None, None, 3.0])

    def test_a_series_with_no_numbers_at_all_is_refused(self):
        out = call(kind='line', title='t',
                   series=[{'name': 'ghost', 'points': [{'x': 'a', 'y': 'lots'}]}])
        self.assertIn('error', out)
        self.assertIn('ghost', out['error'])


class SeriesCapTests(SimpleTestCase):
    """The caps come from the palette, not from a budget."""

    def test_the_full_palette_is_allowed(self):
        out = call(kind='column', title='t',
                   series=[series(f's{i}', 1, 2) for i in range(CHART_MAX_SERIES)])
        self.assertNotIn('error', out)

    def test_a_ninth_series_is_refused_and_told_what_to_do(self):
        """A ninth colour would have to be invented, and an invented hue is one
        nobody checked for separation under colour-vision deficiency."""
        out = call(kind='column', title='t',
                   series=[series(f's{i}', 1) for i in range(CHART_MAX_SERIES + 1)])
        self.assertIn('error', out)
        self.assertIn('Other', out['error'])

    def test_scatter_caps_lower_because_every_pair_is_compared(self):
        """Adjacent-pair separation is not enough when all series are on screen
        against each other; the palette only clears that bar for three slots."""
        ok = call(kind='scatter', title='t',
                  series=[series(f's{i}', 1, 2)
                          for i in range(CHART_MAX_SERIES_ALL_PAIRS)])
        self.assertNotIn('error', ok)

        too_many = call(kind='scatter', title='t',
                        series=[series(f's{i}', 1, 2)
                                for i in range(CHART_MAX_SERIES_ALL_PAIRS + 1)])
        self.assertIn('error', too_many)

    def test_a_pie_takes_exactly_one_series(self):
        """Its slices are parts of one whole, so a second series has no meaning
        — and the refusal names the chart that does compare several."""
        out = call(kind='pie', title='t', series=[series('a', 1), series('b', 2)])
        self.assertIn('error', out)
        self.assertIn('bar chart', out['error'])

    def test_too_many_points_is_refused_rather_than_thinned(self):
        # Thinning would silently change what the chart claims; the model can
        # aggregate, which is what a person would do.
        out = call(kind='line', title='t',
                   series=[series('a', *range(CHART_MAX_POINTS_PER_SERIES + 5))])
        self.assertIn('error', out)
        self.assertIn('Aggregate', out['error'])


class StackingTests(SimpleTestCase):
    def test_stacking_is_kept_where_it_means_something(self):
        out = call(kind='column', title='t', stacked=True,
                   series=[series('a', 1), series('b', 2)])
        self.assertTrue(out['stacked'])

    def test_stacking_is_dropped_where_it_does_not(self):
        """Passed through and ignored by the renderer, a flag starts lying: the
        spec would claim a stacked chart that is drawn unstacked."""
        out = call(kind='line', title='t', stacked=True, series=[series('a', 1)])
        self.assertFalse(out['stacked'])
