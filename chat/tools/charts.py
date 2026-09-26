"""
The `render_chart` tool: a chart in the conversation.

The spec itself (kinds, limits, validation) lives in `office/charts.py`, where
decks and Word files use the very same validator for a chart on a slide. This
module only declares the tool and hands a validated spec to the client, which
owns every visual decision (`components/chat/ChartArtifact.tsx`).
"""
from __future__ import annotations

import json
from typing import Dict

from office.charts import KINDS, ChartError, build_spec
from workflow_backend.thresholds import CHART_MAX_SERIES, CHART_MAX_SERIES_ALL_PAIRS

from .registry import tool


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
    try:
        spec = build_spec(args)
    except ChartError as exc:
        return json.dumps({'error': str(exc)})

    kind, title, series = spec['kind'], spec['title'], spec['series']
    points = sum(len(s['points']) for s in series)
    return json.dumps({
        **spec,
        'rendered': (
            f'{kind} chart "{title}" with {len(series)} series and {points} '
            f'points is now shown to the user. Do not describe it point by '
            f'point; say what it shows.'
        ),
    })
