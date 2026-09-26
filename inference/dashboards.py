"""
The dashboard spec: what a dashboard's tiles may contain, and its validator.

Shared by the `render_dashboard` / `save_dashboard` tools
(`chat/tools/dashboards.py`) and the Dashboards API (`dashboard_views.py`), so
a dashboard saved from the page and one saved by an agent pass the same
checks. It lives beside the `Dashboard` model rather than in the tool module:
the file store must not import the chat tool folder to validate its own rows.
A chart tile takes exactly `render_chart`'s spec (`office/charts.py`).
"""
from __future__ import annotations

from office.charts import build_spec


TILE_TYPES = ('kpi', 'chart', 'table', 'text')


def validate_spec(spec: dict) -> dict:
    """A validated dashboard spec, or `ValueError` naming what to fix."""
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
