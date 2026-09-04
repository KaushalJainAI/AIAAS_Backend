"""
Charts as data, not as markup.

`render_html_artifact` can draw a chart, and that is the problem. It asks a
language model to be a rendering engine: hand-authored SVG inside a 720x520
frame with no network, so no chart library can load, and every chart comes out
looking like a different product because nothing but the prompt holds them
together. Axis labels drift off the edge, dark mode is whatever the model
remembered, and the same request twice gives two different designs.

So this tool takes **the data and a spec** — what kind of chart, what the axes
mean, which series — and the frontend owns everything visual: the palette, the
type scale, the gridlines, the hover layer, dark mode, the table view. The model
cannot produce an ugly chart because it is not drawing one, and it cannot
produce an inaccessible one because the accessibility lives in the component.

Two limits here are not about cost, and both come from the same fact — that a
categorical palette is a *fixed, validated order* of hues rather than a
generator:

* `CHART_MAX_SERIES` (8) is the length of that order. A ninth series would need
  an invented hue, and an invented hue is one nobody checked for separation
  under colour-vision deficiency.
* `CHART_MAX_SERIES_ALL_PAIRS` (3) applies to scatter, where every series is
  compared against every other at once rather than against its neighbours. The
  palette clears that stricter bar for its first three slots only.

Both refuse rather than truncate. A silently dropped series is a chart that is
quietly wrong about its own subject, and the model can be told to fold the tail
into "Other" — which is the honest thing a person would do anyway.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict

from workflow_backend.thresholds import (
    CHART_MAX_LABEL_CHARS,
    CHART_MAX_POINTS_PER_SERIES,
    CHART_MAX_POINTS_TOTAL,
    CHART_MAX_SERIES,
    CHART_MAX_SERIES_ALL_PAIRS,
)

from .registry import tool

logger = logging.getLogger(__name__)

#: Chart kinds the frontend has a renderer for. A closed set for the same reason
#: `agents/contracts.py` is closed: there is a component per kind or there is
#: not, so an open vocabulary would let the model name a shape nothing can draw.
KINDS = ('bar', 'column', 'line', 'area', 'scatter', 'pie')

#: Kinds whose series are all on screen against each other at once.
_ALL_PAIRS_KINDS = frozenset({'scatter'})

#: Kinds that carry exactly one series, because the marks partition a whole.
_SINGLE_SERIES_KINDS = frozenset({'pie'})


class ChartError(ValueError):
    """The spec cannot be drawn. The message is written for the model."""


def _label(value: Any) -> str:
    return str(value).strip()[:CHART_MAX_LABEL_CHARS]


def _number(value: Any) -> float | None:
    """A float, or None for anything that is not one.

    `None` is kept rather than coerced to zero: a missing measurement and a
    measurement of zero are different facts, and a line chart that draws a gap
    is telling the truth where one that dips to the axis is not.
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    # NaN and infinities have no position on a scale, so they are gaps too.
    if out != out or out in (float('inf'), float('-inf')):
        return None
    return out


def _series(raw: Any, kind: str) -> list[dict[str, Any]]:
    if not isinstance(raw, list) or not raw:
        raise ChartError('Give at least one series, each with a name and points.')

    cap = (CHART_MAX_SERIES_ALL_PAIRS if kind in _ALL_PAIRS_KINDS
           else 1 if kind in _SINGLE_SERIES_KINDS
           else CHART_MAX_SERIES)
    if len(raw) > cap:
        if kind in _SINGLE_SERIES_KINDS:
            raise ChartError(
                f'A {kind} chart shows one series — its slices are parts of a '
                f'single whole. You sent {len(raw)}. Send one, or use a bar '
                f'chart to compare several.'
            )
        raise ChartError(
            f'{len(raw)} series is more than a {kind} chart can distinguish; '
            f'the limit is {cap}. Keep the largest {cap - 1} and group the rest '
            f'as "Other", or split this into more than one chart.'
        )

    out: list[dict[str, Any]] = []
    total_points = 0
    for entry in raw:
        if not isinstance(entry, dict):
            raise ChartError('Each series must be an object with name and points.')

        points_raw = entry.get('points')
        if not isinstance(points_raw, list) or not points_raw:
            raise ChartError(
                f'Series "{_label(entry.get("name") or "?")}" has no points.'
            )
        if len(points_raw) > CHART_MAX_POINTS_PER_SERIES:
            raise ChartError(
                f'Series "{_label(entry.get("name") or "?")}" has '
                f'{len(points_raw)} points; the limit is '
                f'{CHART_MAX_POINTS_PER_SERIES}. Aggregate it first — a chart '
                f'with more marks than pixels is a table made harder to read.'
            )

        points = []
        for point in points_raw:
            if isinstance(point, dict):
                x, y = point.get('x'), point.get('y')
            elif isinstance(point, (list, tuple)) and len(point) == 2:
                x, y = point
            else:
                continue
            points.append({'x': _label(x), 'y': _number(y)})

        if not points:
            raise ChartError(
                f'Series "{_label(entry.get("name") or "?")}" has no usable '
                f'points. Each point needs an x label and a numeric y.'
            )
        if all(p['y'] is None for p in points):
            raise ChartError(
                f'Series "{_label(entry.get("name") or "?")}" has no numeric '
                f'values. y must be a number.'
            )

        total_points += len(points)
        if total_points > CHART_MAX_POINTS_TOTAL:
            raise ChartError(
                f'That is more than {CHART_MAX_POINTS_TOTAL} points in total. '
                f'Aggregate, or chart a smaller slice.'
            )

        out.append({'name': _label(entry.get('name') or f'Series {len(out) + 1}'),
                    'points': points})
    return out


@tool({
    "type": "function",
    "function": {
        "name": "render_chart",
        "description": (
            "Draw a chart from data. Give the numbers and say what kind of "
            "chart it is; the app draws it, so do NOT write SVG or HTML for a "
            "chart. Pick the kind from the data's job: bar/column to compare "
            "amounts across categories, line/area for change over time, "
            "scatter to show a relationship between two measures, pie only for "
            "parts of one whole with a handful of slices. If you are showing a "
            "single number, or exact values matter more than the shape, write "
            "it in the text or as a markdown table instead — not every figure "
            "deserves a chart. Never put two different measures with different "
            "scales in one chart; use two charts."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "kind": {
                    "type": "string",
                    "enum": list(KINDS),
                    "description": (
                        "bar = horizontal categories, column = vertical, "
                        "line/area = over time, scatter = two measures, "
                        "pie = parts of one whole."
                    ),
                },
                "title": {
                    "type": "string",
                    "description": "What the chart shows. A sentence, not a label.",
                },
                "series": {
                    "type": "array",
                    "description": (
                        "One entry per line/group. A pie takes exactly one. "
                        f"At most {CHART_MAX_SERIES} "
                        f"({CHART_MAX_SERIES_ALL_PAIRS} for scatter) — past "
                        "that, group the tail as 'Other'."
                    ),
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {
                                "type": "string",
                                "description": "What this series is. Shown in the legend.",
                            },
                            "points": {
                                "type": "array",
                                "description": "The data, in the order it should be read.",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "x": {
                                            "type": "string",
                                            "description": "Category or time label for this point.",
                                        },
                                        "y": {
                                            "type": "number",
                                            "description": "The value. Omit the point entirely if unknown.",
                                        },
                                    },
                                    "required": ["x", "y"],
                                    "additionalProperties": False,
                                },
                            },
                        },
                        "required": ["name", "points"],
                        "additionalProperties": False,
                    },
                },
                "x_label": {"type": "string", "description": "What the x axis measures."},
                "y_label": {"type": "string", "description": "What the y axis measures, with its unit."},
                "stacked": {
                    "type": "boolean",
                    "description": (
                        "Bar/column/area only: stack series into a total "
                        "instead of drawing them side by side. Only when the "
                        "series really do sum to something meaningful."
                    ),
                },
                "note": {
                    "type": "string",
                    "description": "One line under the chart — a source, a caveat, or what to notice.",
                },
            },
            "required": ["kind", "title", "series"],
            "additionalProperties": False,
        },
    },
}, effect="read", parallel=True)
async def render_chart(args: Dict, context: Dict) -> str:
    """Validate a chart spec and hand it to the client to draw.

    Nothing is rendered here, exactly as `render_html_artifact` renders nothing:
    the server's job is to bound and validate, the frontend's is to draw. The
    difference is what crosses the boundary — markup there, data here — and
    that is the whole reason this tool exists.

    Errors come back as a readable `error` rather than an exception, because
    every one of them is something the model can fix on its next turn: fold a
    series into "Other", aggregate the points, pick a different kind.
    """
    kind = str(args.get('kind') or '').strip().lower()
    if kind not in KINDS:
        return json.dumps({
            'error': f'Unknown chart kind {kind!r}. Use one of: {", ".join(KINDS)}.'
        })

    title = _label(args.get('title'))
    if not title:
        return json.dumps({'error': 'Give the chart a title saying what it shows.'})

    try:
        series = _series(args.get('series'), kind)
    except ChartError as exc:
        return json.dumps({'error': str(exc)})

    spec = {
        'type': 'chart',
        'kind': kind,
        'title': title,
        'series': series,
        'x_label': _label(args.get('x_label')),
        'y_label': _label(args.get('y_label')),
        # Stacking is meaningless where series are not additive, so it is
        # dropped rather than passed through and ignored by the renderer —
        # a flag the client silently disregards is how a spec starts lying.
        'stacked': bool(args.get('stacked')) and kind in ('bar', 'column', 'area'),
        'note': _label(args.get('note')),
    }

    points = sum(len(s['points']) for s in series)
    return json.dumps({
        **spec,
        'rendered': (
            f'{kind} chart "{title}" with {len(series)} series and {points} '
            f'points is now shown to the user. Do not describe it point by '
            f'point; say what it shows.'
        ),
    })
