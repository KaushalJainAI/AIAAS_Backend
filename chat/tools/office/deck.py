"""
`.pptx` from a spec: a list of slides, each one of a fixed set of layouts.

The model chooses *what each slide says and which layout says it*; this module
decides everything else — geometry, type sizes, colours, bullets, how a chart
is styled. That split is the whole design, for the reason `render_chart` gives:
a model asked to lay out a slide produces text off the edge, a different look
on every slide and a different product on every deck. A layout vocabulary it
fills in cannot.

Every slide is built on the blank layout and drawn from scratch rather than
filled into a template's placeholders, so the look lives in this file (and in
`themes.py`) rather than in a binary `.potx` nobody can review in a diff.

Charts are **native PowerPoint charts** built from the same spec `render_chart`
validates (`charts.build_spec`), so they stay editable — the numbers are in the
chart, not baked into a picture — and a chart in a deck accepts exactly what a
chart in the conversation does.

Text limits are per layout and refuse rather than shrink. Auto-fitting text by
shrinking the font is how decks end up with a 9-point slide in the middle; a
model told "split this slide" produces a better deck.
"""
from __future__ import annotations

import io
import re
from typing import Any

from lxml import etree
from pptx import Presentation
from pptx.chart.data import CategoryChartData, XyChartData
from pptx.dml.color import RGBColor
from pptx.enum.chart import XL_CHART_TYPE, XL_LEGEND_POSITION
from pptx.enum.shapes import MSO_SHAPE
from pptx.enum.text import MSO_ANCHOR, PP_ALIGN
from pptx.oxml.ns import qn
from pptx.util import Emu, Inches, Pt

from ..charts import ChartError, build_spec
from .spec import SpecError, choice, items, text
from .themes import DEFAULT_THEME, THEME_NAMES, THEMES, Theme

LAYOUTS = ('title', 'section', 'bullets', 'two_column', 'chart', 'image',
           'table', 'quote', 'stats', 'closing')

MAX_SLIDES = 40
MAX_BULLETS = 6
BULLET_CHARS = 160
TITLE_CHARS = 90
SUBTITLE_CHARS = 200
HEADING_CHARS = 60
NOTES_CHARS = 3000
TABLE_COLS = 6
TABLE_ROWS = 10
CELL_CHARS = 60
QUOTE_CHARS = 300
MAX_STATS = 4
STAT_VALUE_CHARS = 14
STAT_LABEL_CHARS = 60
CAPTION_CHARS = 160

SLIDE_W = Inches(13.333)
SLIDE_H = Inches(7.5)
MARGIN = Inches(0.7)
CONTENT_TOP = Inches(1.85)
CONTENT_BOTTOM = SLIDE_H - Inches(0.75)

_BOLD = re.compile(r'\*\*(.+?)\*\*')


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

#: Workspace knob (`render_deck.maxSlides`); the constant stays as the floor
#: under a failed overlay read — validation clamps the knob to never exceed it.
def validate(args: dict, *, max_slides: int = MAX_SLIDES) -> dict:
    """The normalised spec, or `SpecError` naming the slide and what to fix."""
    theme = choice(args.get('theme'), 'theme', THEME_NAMES, DEFAULT_THEME)
    slides_raw = items(args.get('slides'), 'slides',
                       max(1, min(max_slides, MAX_SLIDES)), required=True)
    slides = []
    for n, raw in enumerate(slides_raw, 1):
        if not isinstance(raw, dict):
            raise SpecError(f'Slide {n} must be an object with a layout.')
        layout = choice(raw.get('layout'), f'Slide {n} layout', LAYOUTS, 'bullets')
        try:
            slides.append(_slide(raw, layout))
        except SpecError as exc:
            raise SpecError(f'Slide {n} ({layout}): {exc}') from None
    title = text(args.get('title'), 'title', TITLE_CHARS) or slides[0].get('title', '')
    return {'title': title, 'theme': theme, 'slides': slides}


def _slide(raw: dict, layout: str) -> dict:
    s: dict[str, Any] = {'layout': layout}
    notes = text(raw.get('notes'), 'notes', NOTES_CHARS)
    if notes:
        s['notes'] = notes

    if layout == 'quote':
        s['quote'] = text(raw.get('quote'), 'quote', QUOTE_CHARS, required=True)
        s['attribution'] = text(raw.get('attribution'), 'attribution', HEADING_CHARS)
        return s

    if layout == 'chart':
        try:
            s['chart'] = build_spec(raw.get('chart'))
        except ChartError as exc:
            raise SpecError(f'chart: {exc}') from None
        if s['chart']['kind'] == 'scatter':
            for series in s['chart']['series']:
                for p in series['points']:
                    if _float(p['x']) is None:
                        raise SpecError('a scatter chart needs numeric x values.')
        s['title'] = text(raw.get('title') or s['chart']['title'], 'title', TITLE_CHARS,
                          required=True)
        s['caption'] = text(raw.get('caption') or s['chart'].get('note'), 'caption',
                            CAPTION_CHARS)
        return s

    s['title'] = text(raw.get('title'), 'title', TITLE_CHARS, required=True)

    if layout in ('title', 'section', 'closing'):
        s['subtitle'] = text(raw.get('subtitle'), 'subtitle', SUBTITLE_CHARS)
    elif layout == 'bullets':
        s['bullets'] = _bullets(raw.get('bullets'), 'bullets', required=True)
    elif layout == 'two_column':
        for side in ('left', 'right'):
            col = raw.get(side)
            if not isinstance(col, dict):
                raise SpecError(f'{side} must be an object with heading and bullets.')
            s[side] = {
                'heading': text(col.get('heading'), f'{side}.heading', HEADING_CHARS),
                'bullets': _bullets(col.get('bullets'), f'{side}.bullets', required=True,
                                    limit=5),
            }
    elif layout == 'image':
        s['image'] = text(raw.get('image'), 'image', 500, required=True)
        s['caption'] = text(raw.get('caption'), 'caption', CAPTION_CHARS)
    elif layout == 'table':
        columns = [text(c, 'column header', CELL_CHARS, required=True)
                   for c in items(raw.get('columns'), 'columns', TABLE_COLS, required=True)]
        rows = []
        for r, row in enumerate(items(raw.get('rows'), 'rows', TABLE_ROWS, required=True), 1):
            if not isinstance(row, list) or len(row) > len(columns):
                raise SpecError(f'row {r} must be a list of at most {len(columns)} values.')
            rows.append([text(v, f'row {r} cell', CELL_CHARS) for v in row]
                        + [''] * (len(columns) - len(row)))
        s['columns'], s['rows'] = columns, rows
    elif layout == 'stats':
        stats = []
        for i, st in enumerate(items(raw.get('stats'), 'stats', MAX_STATS, required=True), 1):
            if not isinstance(st, dict):
                raise SpecError(f'stat {i} must be an object with value and label.')
            stats.append({
                'value': text(st.get('value'), f'stat {i} value', STAT_VALUE_CHARS, required=True),
                'label': text(st.get('label'), f'stat {i} label', STAT_LABEL_CHARS, required=True),
            })
        s['stats'] = stats
    return s


def _bullets(raw: Any, field: str, *, required: bool, limit: int = MAX_BULLETS) -> list[dict]:
    out = []
    for i, b in enumerate(items(raw, field, limit, required=required), 1):
        if isinstance(b, dict):
            level = 1 if b.get('level') in (1, '1') else 0
            body = text(b.get('text'), f'{field} {i}', BULLET_CHARS, required=True)
        else:
            # "- " marks a sub-point: a flat list of strings is the shape a
            # model writes most reliably, so nesting rides inside it.
            level = 1 if isinstance(b, str) and b.lstrip().startswith('- ') else 0
            body = text(b.lstrip()[2:] if level else b, f'{field} {i}', BULLET_CHARS,
                        required=True)
        if level and not out:
            raise SpecError(f'{field} 1 is a sub-point ("- ") with no point above it.')
        out.append({'text': body, 'level': level})
    return out


def image_paths(spec: dict) -> list[str]:
    """Every image the deck embeds, so the caller can load them in one pass."""
    return [s['image'] for s in spec['slides'] if s['layout'] == 'image']


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def render(spec: dict, images: dict[str, bytes]) -> bytes:
    theme = THEMES[spec['theme']]
    prs = Presentation()
    prs.slide_width, prs.slide_height = SLIDE_W, SLIDE_H
    blank = prs.slide_layouts[6]

    for n, s in enumerate(spec['slides'], 1):
        slide = prs.slides.add_slide(blank)
        fill = slide.background.fill
        fill.solid()
        fill.fore_color.rgb = _rgb(theme.background)
        _BUILDERS[s['layout']](slide, s, theme, images)
        if s['layout'] != 'title':
            _footer(slide, n, theme)
        if s.get('notes'):
            slide.notes_slide.notes_text_frame.text = s['notes']

    prs.core_properties.title = spec['title']
    buf = io.BytesIO()
    prs.save(buf)
    return buf.getvalue()


def _rgb(hex_color: str) -> RGBColor:
    return RGBColor.from_string(hex_color)


def _float(value: Any) -> float | None:
    try:
        out = float(str(value).replace(',', ''))
    except (TypeError, ValueError):
        return None
    return out if out == out else None


def _rect(slide, left, top, width, height, color: str, shape=MSO_SHAPE.RECTANGLE):
    shp = slide.shapes.add_shape(shape, left, top, width, height)
    shp.fill.solid()
    shp.fill.fore_color.rgb = _rgb(color)
    shp.line.fill.background()
    shp.shadow.inherit = False
    return shp


def _frame(slide, left, top, width, height, *, anchor=MSO_ANCHOR.TOP):
    tf = slide.shapes.add_textbox(left, top, width, height).text_frame
    tf.word_wrap = True
    tf.vertical_anchor = anchor
    tf.margin_left = tf.margin_right = tf.margin_top = tf.margin_bottom = 0
    return tf


def _runs(paragraph, body: str, *, size: float, color: str, font: str,
          bold: bool = False, italic: bool = False) -> None:
    """Add `body` to `paragraph`, honouring `**bold**` spans."""
    pos = 0
    for m in _BOLD.finditer(body):
        if m.start() > pos:
            _run(paragraph, body[pos:m.start()], size, color, font, bold, italic)
        _run(paragraph, m.group(1), size, color, font, True, italic)
        pos = m.end()
    if pos < len(body):
        _run(paragraph, body[pos:], size, color, font, bold, italic)


def _run(paragraph, body, size, color, font, bold, italic):
    r = paragraph.add_run()
    r.text = body
    f = r.font
    f.size, f.bold, f.italic, f.name = Pt(size), bold, italic, font
    f.color.rgb = _rgb(color)


def _para(tf, first: list[bool]):
    """The frame's first paragraph once, then new ones."""
    if first[0]:
        first[0] = False
        return tf.paragraphs[0]
    return tf.add_paragraph()


def _bullet_format(paragraph, level: int, color: str) -> None:
    """A real hanging-indent bullet, which a plain text box does not have."""
    pPr = paragraph._p.get_or_add_pPr()
    step = Inches(0.34)
    pPr.set('marL', str(int(step * (level + 1))))
    pPr.set('indent', str(-int(step)))
    clr = etree.SubElement(pPr, qn('a:buClr'))
    etree.SubElement(clr, qn('a:srgbClr')).set('val', color)
    etree.SubElement(pPr, qn('a:buFont')).set('typeface', 'Arial')
    etree.SubElement(pPr, qn('a:buChar')).set('char', '•' if level == 0 else '–')


def _title(slide, s: dict, theme: Theme) -> None:
    if theme.accent_title:
        _rect(slide, 0, 0, Inches(0.18), SLIDE_H, theme.accent)
    tf = _frame(slide, MARGIN, Inches(0.5), SLIDE_W - 2 * MARGIN, Inches(1.0),
                anchor=MSO_ANCHOR.BOTTOM)
    _runs(tf.paragraphs[0], s['title'], size=32, color=theme.text,
          font=theme.heading_font, bold=True)
    _rect(slide, MARGIN, Inches(1.58), Inches(0.9), Pt(4), theme.accent)


def _footer(slide, n: int, theme: Theme) -> None:
    tf = _frame(slide, SLIDE_W - MARGIN - Inches(1), SLIDE_H - Inches(0.5),
                Inches(1), Inches(0.3), anchor=MSO_ANCHOR.MIDDLE)
    p = tf.paragraphs[0]
    p.alignment = PP_ALIGN.RIGHT
    _run(p, str(n), 11, theme.muted, theme.body_font, False, False)


def _bullet_list(tf, bullets: list[dict], theme: Theme, *, size: float) -> None:
    first = [True]
    for b in bullets:
        p = _para(tf, first)
        p.space_after = Pt(12 if b['level'] == 0 else 6)
        p.line_spacing = 1.1
        level_size = size if b['level'] == 0 else size - 3
        _bullet_format(p, b['level'], theme.accent if b['level'] == 0 else theme.muted)
        _runs(p, b['text'], size=level_size,
              color=theme.text if b['level'] == 0 else theme.muted, font=theme.body_font)


# -- layouts ----------------------------------------------------------------

def _title_slide(slide, s, theme, images, *, closing: bool = False):
    if theme.accent_title:
        _rect(slide, 0, 0, SLIDE_W, SLIDE_H, theme.accent)
        head, sub = theme.on_accent, theme.on_accent
    else:
        _rect(slide, MARGIN, Inches(2.45), Inches(0.14), Inches(2.4), theme.accent)
        head, sub = theme.text, theme.muted
    left = MARGIN + Inches(0.45)
    width = SLIDE_W - left - MARGIN
    tf = _frame(slide, left, Inches(2.3), width, Inches(1.7), anchor=MSO_ANCHOR.BOTTOM)
    _runs(tf.paragraphs[0], s['title'], size=44 if closing else 48, color=head,
          font=theme.heading_font, bold=True)
    if s.get('subtitle'):
        tf = _frame(slide, left, Inches(4.15), width, Inches(1.2))
        _runs(tf.paragraphs[0], s['subtitle'], size=22, color=sub, font=theme.body_font)


def _closing_slide(slide, s, theme, images):
    _title_slide(slide, s, theme, images, closing=True)


def _section_slide(slide, s, theme, images):
    _rect(slide, MARGIN, Inches(2.9), Inches(1.2), Pt(5), theme.accent)
    tf = _frame(slide, MARGIN, Inches(3.1), SLIDE_W - 2 * MARGIN, Inches(1.3))
    _runs(tf.paragraphs[0], s['title'], size=40, color=theme.text,
          font=theme.heading_font, bold=True)
    if s.get('subtitle'):
        tf = _frame(slide, MARGIN, Inches(4.4), SLIDE_W - 2 * MARGIN, Inches(1.0))
        _runs(tf.paragraphs[0], s['subtitle'], size=20, color=theme.muted,
              font=theme.body_font)


def _bullets_slide(slide, s, theme, images):
    _title(slide, s, theme)
    n = len(s['bullets'])
    tf = _frame(slide, MARGIN, CONTENT_TOP + Inches(0.1), SLIDE_W - 2 * MARGIN,
                CONTENT_BOTTOM - CONTENT_TOP)
    _bullet_list(tf, s['bullets'], theme, size=26 if n <= 3 else 22 if n <= 5 else 20)


def _two_column_slide(slide, s, theme, images):
    _title(slide, s, theme)
    gap = Inches(0.6)
    width = (SLIDE_W - 2 * MARGIN - gap) // 2
    for i, side in enumerate(('left', 'right')):
        col = s[side]
        left = MARGIN + i * (width + gap)
        top = CONTENT_TOP + Inches(0.1)
        if col['heading']:
            tf = _frame(slide, left, top, width, Inches(0.5))
            _runs(tf.paragraphs[0], col['heading'], size=22, color=theme.accent,
                  font=theme.heading_font, bold=True)
            top += Inches(0.65)
        tf = _frame(slide, left, top, width, CONTENT_BOTTOM - top)
        _bullet_list(tf, col['bullets'], theme, size=19)


_CATEGORY_TYPES = {
    ('bar', False): XL_CHART_TYPE.BAR_CLUSTERED,
    ('bar', True): XL_CHART_TYPE.BAR_STACKED,
    ('column', False): XL_CHART_TYPE.COLUMN_CLUSTERED,
    ('column', True): XL_CHART_TYPE.COLUMN_STACKED,
    ('line', False): XL_CHART_TYPE.LINE_MARKERS,
    ('area', False): XL_CHART_TYPE.AREA,
    ('area', True): XL_CHART_TYPE.AREA_STACKED,
    ('pie', False): XL_CHART_TYPE.PIE,
}


def _chart_slide(slide, s, theme, images):
    _title(slide, s, theme)
    spec = s['chart']
    caption_h = Inches(0.45) if s.get('caption') else 0
    left, top = MARGIN, CONTENT_TOP
    width, height = SLIDE_W - 2 * MARGIN, CONTENT_BOTTOM - CONTENT_TOP - caption_h
    add_chart(slide.shapes, spec, theme, left, top, width, height)
    if s.get('caption'):
        tf = _frame(slide, MARGIN, CONTENT_BOTTOM - caption_h + Inches(0.08),
                    SLIDE_W - 2 * MARGIN, caption_h)
        _runs(tf.paragraphs[0], s['caption'], size=13, color=theme.muted,
              font=theme.body_font, italic=True)


def add_chart(shapes, spec: dict, theme: Theme, left, top, width, height):
    """A native, editable chart drawn from a `render_chart` spec."""
    kind = spec['kind']
    if kind == 'scatter':
        data = XyChartData()
        for series in spec['series']:
            ser = data.add_series(series['name'])
            for p in series['points']:
                if p['y'] is not None:
                    ser.add_data_point(_float(p['x']), p['y'])
        chart_type = XL_CHART_TYPE.XY_SCATTER
    else:
        # Categories are the union of every series' x labels, in first-seen
        # order; a series missing one gets a gap (None), never a zero.
        categories: list[str] = []
        for series in spec['series']:
            for p in series['points']:
                if p['x'] not in categories:
                    categories.append(p['x'])
        data = CategoryChartData()
        data.categories = categories
        for series in spec['series']:
            by_x = {p['x']: p['y'] for p in series['points']}
            data.add_series(series['name'], [by_x.get(c) for c in categories])
        chart_type = _CATEGORY_TYPES[(kind, bool(spec.get('stacked')))]

    chart = shapes.add_chart(chart_type, left, top, width, height, data).chart
    _transparent(chart)
    chart.font.size = Pt(13)
    chart.font.name = theme.body_font
    chart.font.color.rgb = _rgb(theme.text)
    chart.has_title = False  # the slide title already says what it shows
    many = len(spec['series']) > 1 or kind == 'pie'
    chart.has_legend = many
    if many:
        chart.legend.position = XL_LEGEND_POSITION.BOTTOM
        chart.legend.include_in_layout = False

    plot = chart.plots[0]
    if kind == 'pie':
        for i, point in enumerate(plot.series[0].points):
            point.format.fill.solid()
            point.format.fill.fore_color.rgb = _rgb(theme.palette[i % len(theme.palette)])
        plot.has_data_labels = True
        labels = plot.data_labels
        labels.show_percentage, labels.show_value = True, False
        labels.number_format, labels.number_format_is_linked = '0%', False
        labels.font.size = Pt(12)
        return chart

    if kind in ('bar', 'column'):
        plot.gap_width = 60
        if spec.get('stacked'):
            plot.overlap = 100
    for i, ser in enumerate(plot.series):
        color = _rgb(theme.palette[i % len(theme.palette)])
        if kind in ('line', 'scatter'):
            ser.format.line.color.rgb = color
            ser.format.line.width = Pt(2.5) if kind == 'line' else Pt(0)
            if kind == 'scatter':
                ser.format.line.fill.background()
            ser.marker.format.fill.solid()
            ser.marker.format.fill.fore_color.rgb = color
            ser.marker.format.line.color.rgb = color
            ser.smooth = False
        else:
            ser.format.fill.solid()
            ser.format.fill.fore_color.rgb = color
            ser.format.line.fill.background()

    value_axis, category_axis = chart.value_axis, chart.category_axis
    if kind == 'bar':
        # A horizontal bar chart draws its first category at the bottom; the
        # spec's order is reading order, which is top to bottom.
        category_axis.reverse_order = True
    value_axis.has_major_gridlines = True
    value_axis.major_gridlines.format.line.color.rgb = _rgb(theme.rule)
    value_axis.format.line.fill.background()
    category_axis.format.line.color.rgb = _rgb(theme.rule)
    category_axis.has_major_gridlines = False
    for axis in (value_axis, category_axis):
        axis.tick_labels.font.color.rgb = _rgb(theme.muted)
        axis.tick_labels.font.size = Pt(12)
    if spec.get('x_label'):
        _axis_title(category_axis, spec['x_label'], theme)
    if spec.get('y_label'):
        _axis_title(value_axis, spec['y_label'], theme)
    return chart


def _axis_title(axis, label: str, theme: Theme) -> None:
    axis.has_title = True
    tf = axis.axis_title.text_frame
    tf.text = label
    font = tf.paragraphs[0].runs[0].font
    font.size, font.bold = Pt(12), False
    font.color.rgb = _rgb(theme.muted)


def _transparent(chart) -> None:
    """No chart-area fill, so the chart sits on the slide in every theme.

    python-pptx exposes no chart-area format, and an absent `c:spPr` is drawn
    white by PowerPoint — a white box on the dark theme.
    """
    space = chart._chartSpace
    for old in space.findall(qn('c:spPr')):
        space.remove(old)
    sp = etree.Element(qn('c:spPr'))
    etree.SubElement(sp, qn('a:noFill'))
    etree.SubElement(etree.SubElement(sp, qn('a:ln')), qn('a:noFill'))
    space.find(qn('c:chart')).addnext(sp)


def _image_slide(slide, s, theme, images):
    _title(slide, s, theme)
    data = images.get(s['image'])
    caption_h = Inches(0.45) if s.get('caption') else 0
    box_w = SLIDE_W - 2 * MARGIN
    box_h = CONTENT_BOTTOM - CONTENT_TOP - caption_h
    if data:
        from PIL import Image

        with Image.open(io.BytesIO(data)) as img:
            iw, ih = img.size
        scale = min(box_w / iw, box_h / ih)
        w, h = Emu(int(iw * scale)), Emu(int(ih * scale))
        slide.shapes.add_picture(io.BytesIO(data), MARGIN + (box_w - w) // 2,
                                 CONTENT_TOP + (box_h - h) // 2, w, h)
    if s.get('caption'):
        tf = _frame(slide, MARGIN, CONTENT_BOTTOM - caption_h + Inches(0.08), box_w, caption_h)
        p = tf.paragraphs[0]
        p.alignment = PP_ALIGN.CENTER
        _runs(p, s['caption'], size=13, color=theme.muted, font=theme.body_font, italic=True)


def _table_slide(slide, s, theme, images):
    _title(slide, s, theme)
    columns, rows = s['columns'], s['rows']
    row_h = Inches(0.46)
    width = SLIDE_W - 2 * MARGIN
    shape = slide.shapes.add_table(len(rows) + 1, len(columns), MARGIN, CONTENT_TOP,
                                   width, row_h * (len(rows) + 1))
    table = shape.table
    # Drop the built-in style's banding: every colour here comes from the theme.
    tbl_pr = shape._element.graphic.graphicData.tbl.tblPr
    for attr in ('bandRow', 'firstRow'):
        tbl_pr.set(attr, '0')
    for c, header in enumerate(columns):
        _cell(table.cell(0, c), header, theme.accent, theme.on_accent, theme, bold=True)
    for r, row in enumerate(rows, 1):
        fill = theme.surface if r % 2 == 0 else theme.background
        for c, value in enumerate(row):
            _cell(table.cell(r, c), value, fill, theme.text, theme)


def _cell(cell, value: str, fill: str, color: str, theme: Theme, *, bold: bool = False):
    cell.fill.solid()
    cell.fill.fore_color.rgb = _rgb(fill)
    cell.margin_left = cell.margin_right = Inches(0.12)
    cell.vertical_anchor = MSO_ANCHOR.MIDDLE
    tf = cell.text_frame
    tf.word_wrap = True
    p = tf.paragraphs[0]
    _runs(p, value, size=14, color=color, font=theme.body_font, bold=bold)


def _quote_slide(slide, s, theme, images):
    tf = _frame(slide, MARGIN + Inches(0.4), Inches(1.0), Inches(1.5), Inches(1.5))
    _run(tf.paragraphs[0], '“', 120, theme.accent, theme.heading_font, True, False)
    tf = _frame(slide, MARGIN + Inches(1.2), Inches(2.2), SLIDE_W - 2 * MARGIN - Inches(1.6),
                Inches(3.0), anchor=MSO_ANCHOR.MIDDLE)
    _runs(tf.paragraphs[0], s['quote'], size=30, color=theme.text,
          font=theme.heading_font, italic=True)
    if s.get('attribution'):
        tf = _frame(slide, MARGIN + Inches(1.2), Inches(5.35), SLIDE_W - 2 * MARGIN - Inches(1.6),
                    Inches(0.6))
        _runs(tf.paragraphs[0], f'— {s["attribution"]}', size=18, color=theme.muted,
              font=theme.body_font)


def _stats_slide(slide, s, theme, images):
    _title(slide, s, theme)
    stats = s['stats']
    gap = Inches(0.4)
    width = (SLIDE_W - 2 * MARGIN - gap * (len(stats) - 1)) // len(stats)
    top, height = CONTENT_TOP + Inches(0.5), Inches(3.2)
    for i, st in enumerate(stats):
        left = MARGIN + i * (width + gap)
        _rect(slide, left, top, width, height, theme.surface)
        _rect(slide, left, top, width, Pt(5), theme.accent)
        tf = _frame(slide, left + Inches(0.3), top + Inches(0.5), width - Inches(0.6), Inches(1.3),
                    anchor=MSO_ANCHOR.BOTTOM)
        _runs(tf.paragraphs[0], st['value'], size=48 if len(stats) < 4 else 40,
              color=theme.accent, font=theme.heading_font, bold=True)
        tf = _frame(slide, left + Inches(0.3), top + Inches(1.95), width - Inches(0.6), Inches(1.1))
        _runs(tf.paragraphs[0], st['label'], size=17, color=theme.muted, font=theme.body_font)


_BUILDERS = {
    'title': _title_slide,
    'section': _section_slide,
    'bullets': _bullets_slide,
    'two_column': _two_column_slide,
    'chart': _chart_slide,
    'image': _image_slide,
    'table': _table_slide,
    'quote': _quote_slide,
    'stats': _stats_slide,
    'closing': _closing_slide,
}


# ---------------------------------------------------------------------------
# What is stored besides the bytes
# ---------------------------------------------------------------------------

def preview(spec: dict) -> dict:
    """The spec plus the theme's tokens, so the preview draws in the same colours."""
    t = THEMES[spec['theme']]
    return {
        'kind': 'deck',
        'title': spec['title'],
        'theme': {
            'name': t.name, 'background': t.background, 'surface': t.surface,
            'text': t.text, 'muted': t.muted, 'accent': t.accent,
            'on_accent': t.on_accent, 'accent_title': t.accent_title,
            'rule': t.rule, 'palette': list(t.palette),
        },
        'slides': spec['slides'],
    }


def extract_text(spec: dict) -> str:
    out: list[str] = []
    for n, s in enumerate(spec['slides'], 1):
        out.append(f'Slide {n} [{s["layout"]}]: {s.get("title") or s.get("quote", "")}')
        if s.get('subtitle'):
            out.append(s['subtitle'])
        for b in s.get('bullets', []):
            out.append(('  - ' if b['level'] else '- ') + b['text'])
        for side in ('left', 'right'):
            if side in s:
                if s[side]['heading']:
                    out.append(s[side]['heading'])
                out.extend('- ' + b['text'] for b in s[side]['bullets'])
        if 'columns' in s:
            out.append(' | '.join(s['columns']))
            out.extend(' | '.join(r) for r in s['rows'])
        for st in s.get('stats', []):
            out.append(f'{st["value"]} — {st["label"]}')
        if s.get('attribution'):
            out.append(f'— {s["attribution"]}')
        if 'chart' in s:
            out.append(f'Chart: {s["chart"]["title"]} ({s["chart"]["kind"]})')
        if s.get('caption'):
            out.append(s['caption'])
        if s.get('notes'):
            out.append(f'Notes: {s["notes"]}')
        out.append('')
    return '\n'.join(out)
