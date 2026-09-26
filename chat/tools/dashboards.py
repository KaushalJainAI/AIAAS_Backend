"""
Agent-built dashboards (P8): `render_dashboard` + `save_dashboard`.

`render_dashboard(title, tiles[])` is in ALWAYS_AVAILABLE, like `render_chart`:
tiles are kpi | chart | table | text, and a chart tile takes exactly
`render_chart`'s spec. Rendered by a DashboardArtifact under the chart design
rules: fixed palette, a gap is not a zero, refuse rather than truncate.

`save_dashboard` creates a live `Dashboard`: a tile binds to a source (a
stored query_sql, a Sheet range, a saved GET API call) instead of inline
data. A schedule refreshes the sources with no LLM call. Shareable with the
published-page visibility levels (link < platform < public, same 404 rule).
"""
from __future__ import annotations

import json
import logging
from typing import Dict

from inference.dashboards import validate_spec

from .registry import tool

logger = logging.getLogger(__name__)

@tool({
    'type': 'function',
    'function': {
        'name': 'render_dashboard',
        'description': (
            'Draw a dashboard from tiles: kpi numbers, charts, tables, text. '
            'A chart tile takes exactly render_chart\'s shape. The app draws '
            'it — never write HTML for a dashboard.'
        ),
        'parameters': {
            'type': 'object',
            'properties': {
                'title': {'type': 'string'},
                'tiles': {'type': 'array', 'items': {'type': 'object'}},
            },
            'required': ['title', 'tiles'],
            'additionalProperties': False,
        },
    },
}, effect='read')
async def render_dashboard(args: Dict, context: Dict) -> str:
    try:
        spec = validate_spec(args)
    except ValueError as exc:
        return json.dumps({'error': str(exc)})
    return json.dumps({'type': 'dashboard', **spec})


@tool({
    'type': 'function',
    'function': {
        'name': 'save_dashboard',
        'description': (
            'Save a live dashboard: tiles bound to sources (a stored query, '
            'a Sheet range, a saved GET call) refreshed on a schedule with no '
            'LLM call. Reads like render_dashboard plus sources and refresh.'
        ),
        'parameters': {
            'type': 'object',
            'properties': {
                'title': {'type': 'string'},
                'tiles': {'type': 'array', 'items': {'type': 'object'}},
                'sources': {'type': 'array', 'items': {'type': 'object'}},
                'refresh_cron': {'type': 'string'},
                'visibility': {'type': 'string', 'enum': ['link', 'platform', 'public']},
            },
            'required': ['title', 'tiles'],
            'additionalProperties': False,
        },
    },
}, effect='reversible')
async def save_dashboard(args: Dict, context: Dict) -> str:
    from asgiref.sync import sync_to_async

    user_id = context.get('user_id')
    if not user_id:
        return json.dumps({'error': 'No user context.'})
    try:
        spec = validate_spec(args)
    except ValueError as exc:
        return json.dumps({'error': str(exc)})
    visibility = str(args.get('visibility') or 'platform').strip().lower()
    if visibility not in ('link', 'platform', 'public'):
        return json.dumps({'error': '`visibility` must be link, platform or public.'})

    def _create():
        from inference.models import Dashboard

        return Dashboard.objects.create(
            user_id=user_id, title=spec['title'], spec=spec,
            sources=args.get('sources') or [],
            refresh_cron=str(args.get('refresh_cron') or ''),
            visibility=visibility,
        )

    try:
        row = await sync_to_async(_create)()
    except Exception:
        logger.exception('[Dashboards] save_dashboard failed')
        return json.dumps({'error': 'The dashboard could not be saved.'})
    return json.dumps({
        'dashboard_id': row.id, 'title': row.title,
        'rendered': f'Saved dashboard "{row.title}".',
    })


DASHBOARD_TOOLS = ('render_dashboard', 'save_dashboard')
