"""
Diagrams as data: boxes and arrows the model describes, drawn here.

Same rule as `render_chart` one floor down — the model says what connects to
what, and this decides geometry, colour and type. Mermaid was the obvious
alternative and is rejected for the reason the sandbox has no plotting
library: rendering it needs a headless browser (Mermaid is JavaScript), which
does not fit beside the app, and a model hand-authoring SVG produces a
different-looking diagram every time with labels off the edge.

The layout is a **layered left-to-right walk**: a node's column is one past its
deepest incoming edge, and nodes in a column are spread evenly down the canvas.
That covers what people actually ask for — flows, pipelines, org steps,
architectures — and it is honest about what it is not: no orthogonal routing,
no crossing minimisation, no cycles-as-loops (a back edge is drawn, and drawn
plainly, rather than rearranging the picture around it).

Output is SVG, which every browser and Word render. Slides take pictures
rather than SVG through python-pptx, so a diagram cannot go on a slide yet —
the tool says so rather than writing a file a deck cannot use.
"""
from __future__ import annotations

from xml.sax.saxutils import escape

from .spec import SpecError, choice, items, text
from .themes import THEMES

MAX_NODES = 24
MAX_EDGES = 40
LABEL_CHARS = 60
EDGE_LABEL_CHARS = 30

#: Geometry, in user units (the SVG scales to its container).
BOX_W, BOX_H = 190, 62
GAP_X, GAP_Y = 90, 34
PAD = 28
SHAPES = ('box', 'round', 'diamond')


def validate(args: dict) -> dict:
    theme_name = choice(args.get('theme'), 'theme', tuple(THEMES), 'clean')
    title = text(args.get('title'), 'title', 120)
    raw_nodes = items(args.get('nodes'), 'nodes', MAX_NODES, required=True)

    nodes: dict[str, dict] = {}
    for i, node in enumerate(raw_nodes, 1):
        if isinstance(node, str):
            node = {'id': node, 'label': node}
        if not isinstance(node, dict):
            raise SpecError(f'Node {i} must be an object with an id and a label.')
        node_id = text(node.get('id'), f'node {i} id', 40, required=True)
        if node_id in nodes:
            raise SpecError(f'Two nodes share the id {node_id!r}. Ids must be unique.')
        nodes[node_id] = {
            'id': node_id,
            'label': text(node.get('label') or node_id, f'node {i} label', LABEL_CHARS),
            'shape': choice(node.get('shape'), f'node {i} shape', SHAPES, 'box'),
            'accent': bool(node.get('accent')),
        }

    edges = []
    for i, edge in enumerate(items(args.get('edges'), 'edges', MAX_EDGES), 1):
        if not isinstance(edge, dict):
            raise SpecError(f'Edge {i} must be an object with from and to.')
        source = text(edge.get('from'), f'edge {i} from', 40, required=True)
        target = text(edge.get('to'), f'edge {i} to', 40, required=True)
        for end in (source, target):
            if end not in nodes:
                raise SpecError(f'Edge {i} names {end!r}, which is not one of the nodes.')
        if source == target:
            raise SpecError(f'Edge {i} points at its own node; a self-loop cannot be drawn here.')
        edges.append({'from': source, 'to': target,
                      'label': text(edge.get('label'), f'edge {i} label', EDGE_LABEL_CHARS)})
    return {'title': title, 'theme': theme_name, 'nodes': list(nodes.values()), 'edges': edges}


def _columns(spec: dict) -> list[list[dict]]:
    """Each node's column: one past its deepest ancestor, cycles broken."""
    by_id = {n['id']: n for n in spec['nodes']}
    incoming: dict[str, list[str]] = {n: [] for n in by_id}
    for edge in spec['edges']:
        incoming[edge['to']].append(edge['from'])

    depth: dict[str, int] = {}

    def resolve(node_id: str, seen: frozenset[str]) -> int:
        if node_id in depth:
            return depth[node_id]
        if node_id in seen:          # a cycle: stop rather than recurse for ever
            return 0
        parents = incoming[node_id]
        value = 0 if not parents else 1 + max(
            resolve(p, seen | {node_id}) for p in parents
        )
        depth[node_id] = value
        return value

    for node_id in by_id:
        resolve(node_id, frozenset())

    width = max(depth.values(), default=0) + 1
    columns: list[list[dict]] = [[] for _ in range(width)]
    for node in spec['nodes']:
        columns[depth[node['id']]].append(node)
    return columns


def _place(spec: dict) -> tuple[dict[str, tuple[float, float]], float, float]:
    columns = _columns(spec)
    tallest = max((len(c) for c in columns), default=1)
    height = PAD * 2 + tallest * BOX_H + (tallest - 1) * GAP_Y
    width = PAD * 2 + len(columns) * BOX_W + (len(columns) - 1) * GAP_X
    at: dict[str, tuple[float, float]] = {}
    for x, column in enumerate(columns):
        span = len(column) * BOX_H + (len(column) - 1) * GAP_Y
        top = (height - span) / 2
        for y, node in enumerate(column):
            at[node['id']] = (PAD + x * (BOX_W + GAP_X), top + y * (BOX_H + GAP_Y))
    return at, width, height


def _wrap(label: str, per_line: int = 24) -> list[str]:
    lines, line = [], ''
    for word in label.split():
        candidate = f'{line} {word}'.strip()
        if len(candidate) > per_line and line:
            lines.append(line)
            line = word
        else:
            line = candidate
    if line:
        lines.append(line)
    return lines[:3]


def render(spec: dict) -> bytes:
    """The diagram as SVG bytes."""
    theme = THEMES[spec['theme']]
    at, width, height = _place(spec)
    title_h = 34 if spec['title'] else 0
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width:.0f} {height + title_h:.0f}" '
        f'width="100%" role="img" aria-label="{escape(spec["title"] or "Diagram")}">',
        f'<rect width="100%" height="100%" fill="#{theme.background}"/>',
        '<defs><marker id="a" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" '
        f'markerHeight="7" orient="auto-start-reverse"><path d="M0,0 L10,5 L0,10 z" '
        f'fill="#{theme.muted}"/></marker></defs>',
    ]
    if spec['title']:
        parts.append(
            f'<text x="{PAD}" y="24" font-family="{theme.heading_font}, sans-serif" '
            f'font-size="17" font-weight="bold" fill="#{theme.text}">{escape(spec["title"])}</text>'
        )

    for edge in spec['edges']:
        x1, y1 = at[edge['from']]
        x2, y2 = at[edge['to']]
        start = (x1 + BOX_W, y1 + BOX_H / 2 + title_h)
        end = (x2, y2 + BOX_H / 2 + title_h)
        if x2 <= x1:   # a back edge: drawn plainly rather than routed around
            start = (x1 + BOX_W / 2, y1 + BOX_H + title_h)
            end = (x2 + BOX_W / 2, y2 + BOX_H + title_h)
        mid_x, mid_y = (start[0] + end[0]) / 2, (start[1] + end[1]) / 2
        parts.append(
            f'<path d="M{start[0]:.0f},{start[1]:.0f} C{mid_x:.0f},{start[1]:.0f} '
            f'{mid_x:.0f},{end[1]:.0f} {end[0]:.0f},{end[1]:.0f}" fill="none" '
            f'stroke="#{theme.rule}" stroke-width="2" marker-end="url(#a)"/>'
        )
        if edge['label']:
            parts.append(
                f'<text x="{mid_x:.0f}" y="{mid_y - 6:.0f}" text-anchor="middle" '
                f'font-family="{theme.body_font}, sans-serif" font-size="11" '
                f'fill="#{theme.muted}">{escape(edge["label"])}</text>'
            )

    for node in spec['nodes']:
        x, y = at[node['id']]
        y += title_h
        fill = f'#{theme.accent}' if node['accent'] else f'#{theme.surface}'
        colour = f'#{theme.on_accent}' if node['accent'] else f'#{theme.text}'
        if node['shape'] == 'diamond':
            cx, cy = x + BOX_W / 2, y + BOX_H / 2
            parts.append(
                f'<polygon points="{cx:.0f},{y:.0f} {x + BOX_W:.0f},{cy:.0f} '
                f'{cx:.0f},{y + BOX_H:.0f} {x:.0f},{cy:.0f}" fill="{fill}" '
                f'stroke="#{theme.rule}" stroke-width="1.5"/>'
            )
        else:
            radius = 26 if node['shape'] == 'round' else 8
            parts.append(
                f'<rect x="{x:.0f}" y="{y:.0f}" width="{BOX_W}" height="{BOX_H}" rx="{radius}" '
                f'fill="{fill}" stroke="#{theme.rule}" stroke-width="1.5"/>'
            )
        lines = _wrap(node['label'])
        first = y + BOX_H / 2 - (len(lines) - 1) * 8
        for i, line in enumerate(lines):
            parts.append(
                f'<text x="{x + BOX_W / 2:.0f}" y="{first + i * 16:.0f}" text-anchor="middle" '
                f'dominant-baseline="middle" font-family="{theme.body_font}, sans-serif" '
                f'font-size="13" fill="{colour}">{escape(line)}</text>'
            )

    parts.append('</svg>')
    return '\n'.join(parts).encode('utf-8')


def extract_text(spec: dict) -> str:
    lines = [spec['title'] or 'Diagram']
    labels = {n['id']: n['label'] for n in spec['nodes']}
    lines.extend(f'- {n["label"]}' for n in spec['nodes'])
    for edge in spec['edges']:
        arrow = f'{labels[edge["from"]]} -> {labels[edge["to"]]}'
        lines.append(f'{arrow} ({edge["label"]})' if edge['label'] else arrow)
    return '\n'.join(lines)
