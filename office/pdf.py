"""
PDF from the same blocks `render_document` takes.

One spec, two outputs: a `.docx` is what someone edits, a `.pdf` is what they
send, print or attach — and asking the model to describe the same report twice
is how the two drift apart. `document.validate` is reused verbatim, so every
limit, every refusal and every inline `**bold**` rule holds here too.

Drawn with ReportLab rather than by converting the Word file: a conversion
needs LibreOffice, which is a 400 MB dependency and a subprocess per render on
a box with 913 MB of RAM. The cost is that the two renderers must be kept in
step by hand — which is why they share the spec and the tests assert on the
same blocks.
"""
from __future__ import annotations

import io
import re
from typing import Any

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_JUSTIFY, TA_LEFT, TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
    Image, KeepTogether, ListFlowable, ListItem, PageBreak, Paragraph,
    SimpleDocTemplate, Spacer, Table, TableStyle,
)

from .themes import THEMES

#: ReportLab's own markup is a small HTML subset, so every string is escaped
#: before `**bold**` / `*italic*` are turned into tags — a document quoting a
#: web page must not be able to inject markup into the PDF.
_BOLD = re.compile(r'\*\*(.+?)\*\*')
_ITALIC = re.compile(r'(?<!\*)\*([^*\s][^*]*?)\*(?!\*)')


def _escape(text: str) -> str:
    return str(text).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')


def _rich(text: str) -> str:
    escaped = _escape(text)
    escaped = _BOLD.sub(r'<b>\1</b>', escaped)
    return _ITALIC.sub(r'<i>\1</i>', escaped)


def _rich_runs(runs: list) -> str:
    """Typed runs to reportlab markup. Run text is literal (no markers)."""
    parts = []
    for run in runs:
        body = _escape(run.get('text', ''))
        if run.get('code'):
            body = f'<font face="Courier">{body}</font>'
        if run.get('bold'):
            body = f'<b>{body}</b>'
        if run.get('italic'):
            body = f'<i>{body}</i>'
        if run.get('underline'):
            body = f'<u>{body}</u>'
        if run.get('strike'):
            body = f'<strike>{body}</strike>'
        if run.get('link'):
            body = f'<a href="{_escape(run["link"])}">{body}</a>'
        parts.append(body)
    return ''.join(parts)


def _rich_block(block: dict) -> str:
    return _rich_runs(block['runs']) if block.get('runs') is not None else _rich(block.get('text', ''))


def _rich_item(item) -> str:
    if isinstance(item, dict):
        return _rich_block(item)
    return _rich(str(item))


def _styles(theme) -> dict[str, ParagraphStyle]:
    base = getSampleStyleSheet()
    accent = colors.HexColor('#' + theme.accent)
    text = colors.HexColor('#' + theme.text)
    muted = colors.HexColor('#' + theme.muted)
    body = ParagraphStyle('Body', parent=base['BodyText'], fontName='Helvetica',
                          fontSize=10.5, leading=15, textColor=text, alignment=TA_LEFT,
                          spaceAfter=6)
    return {
        'title': ParagraphStyle('DocTitle', parent=body, fontName='Helvetica-Bold',
                                fontSize=22, leading=27, spaceAfter=4),
        'subtitle': ParagraphStyle('DocSubtitle', parent=body, fontSize=12.5,
                                   leading=17, textColor=muted, spaceAfter=14),
        'h1': ParagraphStyle('H1', parent=body, fontName='Helvetica-Bold', fontSize=15,
                             leading=20, textColor=accent, spaceBefore=14, spaceAfter=4),
        'h2': ParagraphStyle('H2', parent=body, fontName='Helvetica-Bold', fontSize=12.5,
                             leading=17, spaceBefore=11, spaceAfter=3),
        'h3': ParagraphStyle('H3', parent=body, fontName='Helvetica-Bold', fontSize=11,
                             leading=15, spaceBefore=9, spaceAfter=2),
        'body': body,
        'quote': ParagraphStyle('Quote', parent=body, leftIndent=12, textColor=muted,
                                fontName='Helvetica-Oblique', borderPadding=0),
        'caption': ParagraphStyle('Caption', parent=body, fontSize=8.5, leading=12,
                                  textColor=muted, spaceBefore=2),
        'cell': ParagraphStyle('Cell', parent=body, fontSize=9, leading=12, spaceAfter=0),
        'head': ParagraphStyle('CellHead', parent=body, fontName='Helvetica-Bold',
                               fontSize=9, leading=12, spaceAfter=0,
                               textColor=colors.HexColor('#' + theme.on_accent)),
    }


def _table(block: dict, styles: dict, theme) -> list:
    header = [Paragraph(_rich(h), styles['head']) for h in block['columns']]
    rows = [[Paragraph(_rich(c), styles['cell']) for c in row] for row in block['rows']]
    table = Table([header, *rows], repeatRows=1, hAlign='LEFT')
    table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#' + theme.accent)),
        ('ROWBACKGROUNDS', (0, 1), (-1, -1),
         [colors.white, colors.HexColor('#' + theme.surface)]),
        ('GRID', (0, 0), (-1, -1), 0.4, colors.HexColor('#' + theme.rule)),
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('LEFTPADDING', (0, 0), (-1, -1), 5),
        ('RIGHTPADDING', (0, 0), (-1, -1), 5),
        ('TOPPADDING', (0, 0), (-1, -1), 4),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
    ]))
    out: list[Any] = [table]
    if block.get('caption'):
        out.append(Paragraph(_rich(block['caption']), styles['caption']))
    return out


def _chart_as_table(block: dict, styles: dict, theme) -> list:
    """The same fallback `render_document` makes, for the same reason."""
    spec = block['chart']
    xs: list[str] = []
    for series in spec['series']:
        for point in series['points']:
            if point['x'] not in xs:
                xs.append(point['x'])
    lookup = [{p['x']: p['y'] for p in s['points']} for s in spec['series']]
    rows = [[x] + [('—' if m.get(x) is None else f'{m[x]:,.2f}'.rstrip('0').rstrip('.'))
                   for m in lookup] for x in xs]
    return [
        Paragraph(_rich(spec['title']), styles['h3']),
        *_table({'columns': [spec.get('x_label') or 'Category',
                             *[s['name'] for s in spec['series']]],
                 'rows': rows,
                 'caption': (spec.get('note') + ' ' if spec.get('note') else '')
                            + '(Chart data shown as a table.)'}, styles, theme),
    ]


_ALIGNMENTS = {'left': TA_LEFT, 'center': TA_CENTER,
               'right': TA_RIGHT, 'justify': TA_JUSTIFY}


def _aligned(base: ParagraphStyle, align: str | None) -> ParagraphStyle:
    if not align or align == 'left':
        return base
    return ParagraphStyle(f'{base.name}-{align}', parent=base,
                          alignment=_ALIGNMENTS[align])


def render(spec: dict, images: dict[str, bytes]) -> bytes:
    """The PDF's bytes, from a `document.validate`d spec."""
    theme = THEMES[spec['theme']]
    styles = _styles(theme)
    story: list[Any] = [Paragraph(_rich(spec['title']), styles['title'])]
    if spec['subtitle']:
        story.append(Paragraph(_rich(spec['subtitle']), styles['subtitle']))

    for block in spec['blocks']:
        kind = block['type']
        align = block.get('align')
        if kind == 'heading':
            story.append(Paragraph(_rich_block(block),
                                   _aligned(styles[f'h{block["level"]}'], align)))
        elif kind == 'paragraph':
            story.append(Paragraph(_rich_block(block), _aligned(styles['body'], align)))
        elif kind == 'quote':
            story.append(Paragraph(_rich_block(block), _aligned(styles['quote'], align)))
        elif kind in ('bullets', 'numbered'):
            story.append(ListFlowable(
                [ListItem(Paragraph(_rich_item(item), styles['body']), leftIndent=14)
                 for item in block['items']],
                bulletType='bullet' if kind == 'bullets' else '1',
                bulletFontSize=8, leftIndent=14,
            ))
        elif kind == 'table':
            story.extend(_table(block, styles, theme))
        elif kind == 'chart':
            story.extend(_chart_as_table(block, styles, theme))
        elif kind == 'image':
            data = images.get(block['path'])
            if data:
                story.append(KeepTogether(_image(data, block, styles)))
            elif block['caption']:
                story.append(Paragraph(_rich(block['caption']), styles['caption']))
        elif kind == 'page_break':
            story.append(PageBreak())
        story.append(Spacer(1, 2))

    buf = io.BytesIO()
    SimpleDocTemplate(
        buf, pagesize=A4, title=spec['title'],
        leftMargin=22 * mm, rightMargin=22 * mm, topMargin=20 * mm, bottomMargin=20 * mm,
    ).build(story)
    return buf.getvalue()


def _image(data: bytes, block: dict, styles: dict) -> list:
    from PIL import Image as PilImage

    with PilImage.open(io.BytesIO(data)) as img:
        width, height = img.size
    # Fit the text column; never upscale a small image to a blurry full width.
    max_width = (210 - 44) * mm
    scale = min(max_width / width, 1.0)
    out: list[Any] = [Image(io.BytesIO(data), width=width * scale, height=height * scale)]
    if block['caption']:
        out.append(Paragraph(_rich(block['caption']), styles['caption']))
    return out
