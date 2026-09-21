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

from .registry import tool

logger = logging.getLogger(__name__)

TILE_TYPES = ('kpi', 'chart', 'table', 'text')


def _validate_spec(spec: dict) -> dict:
    from chat.tools.charts import build_spec

    title = str(spec.get('title') or '').strip()
    if not title:
        raise ValueError('Give the dashboard a title.')
    tiles_raw = spec.get('tiles') or []
    if not isinstance(tiles_raw, list) or not tiles_raw:
        raise ValueError('Give at least one tile.')
    if len(tiles_raw) > 12:
        raise ValueError('At most 12 tiles. Split it into two dashboards.')
    tiles = []
    for i, raw in enumerate(tiles_raw, 1):
        if not isinstance(raw, dict):
            raise ValueError(f'Tile {i} must be an object.')
        kind = str(raw.get('kind') or '').strip().lower()
        if kind not in TILE_TYPES:
            raise ValueError(f'Tile {i}: kind must be one of {", ".join(TILE_TYPES)}.')
        tile: dict = {'kind': kind, 'title': str(raw.get('title') or '')[:120]}
        if kind == 'kpi':
            tile['value'] = str(raw.get('value') or '')[:60]
            tile['delta'] = str(raw.get('delta') or '')[:60]
        elif kind == 'chart':
            try:
                tile['chart'] = build_spec(raw.get('chart') or {})
            except Exception as exc:  # noqa: BLE001
                raise ValueError(f'Tile {i} chart: {exc}') from exc
        elif kind == 'table':
            columns = raw.get('columns') or []
            rows = raw.get('rows') or []
            if not isinstance(columns, list) or not columns:
                raise ValueError(f'Tile {i}: a table needs columns.')
            tile['columns'] = [str(c)[:80] for c in columns[:12]]
            tile['rows'] = [[str(v)[:200] for v in r[:12]] if isinstance(r, list) else []
                            for r in rows[:50]]
        else:
            tile['text'] = str(raw.get('text') or '')[:2000]
        tiles.append(tile)
    return {'title': title, 'tiles': tiles}


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
        spec = _validate_spec(args)
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
        spec = _validate_spec(args)
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
