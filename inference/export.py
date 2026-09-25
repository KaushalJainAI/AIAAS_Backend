"""
Export: a file in another format, for taking away or saving beside it.

One table (`FORMATS`) says which formats each type offers, and the apps' File
menu, the export route and the `export_file` tool all read it, so a format is
offered exactly where it can be produced.

The renderers are the office tools' own (`chat/tools/office/`), so a Word file
exported as PDF looks like the PDF `render_pdf` would have made from the same
content — one design system, not a second one for exports.
"""
from __future__ import annotations

import csv
import io
from typing import Any

from .models import Document

#: file_type → formats it can be exported as, best first.
FORMATS: dict[str, tuple[str, ...]] = {
    'docx': ('pdf', 'md', 'txt'),
    'md': ('pdf', 'docx'),
    'txt': ('pdf', 'docx'),
    'pptx': ('pdf',),
    'xlsx': ('csv',),
    'csv': ('xlsx',),
}

MIME = {
    'pdf': 'application/pdf',
    'docx': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
    'xlsx': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    'csv': 'text/csv',
    'md': 'text/markdown',
    'txt': 'text/plain',
}


class ExportError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def formats_for(doc: Document) -> tuple[str, ...]:
    return FORMATS.get(doc.file_type or '', ())


def _stem(doc: Document) -> str:
    stem, dot, _ = doc.name.rpartition('.')
    return stem if dot else doc.name


def _text_of(doc: Document) -> str:
    if doc.file:
        try:
            with doc.file.open('rb') as fh:
                return fh.read().decode('utf-8', errors='replace')
        except Exception:  # noqa: BLE001 — fall back to the extract
            pass
    return doc.content_text or ''


def _images(doc: Document, paths: list[str]) -> dict[str, bytes]:
    """Embedded images, best effort: a missing one exports as its caption."""
    from .office_edit import EditError, _images_for

    out: dict[str, bytes] = {}
    for p in paths:
        try:
            out.update(_images_for(doc, [p]))
        except EditError:
            continue
    return out


def _document_spec(doc: Document) -> dict:
    """What a Word / Markdown / text file says, as a renderable spec."""
    from .text_blocks import markdown_blocks, spec_from, text_blocks

    stored = (doc.metadata or {}).get('spec')
    if doc.file_type == 'docx' and isinstance(stored, dict) and stored.get('blocks'):
        return {'title': stored.get('title') or _stem(doc), 'subtitle': stored.get('subtitle') or '',
                'theme': stored.get('theme') if stored.get('theme') in ('clean', 'bold') else 'clean',
                'blocks': stored['blocks']}
    if doc.file_type == 'md':
        title, blocks = markdown_blocks(_text_of(doc))
        return spec_from(title or _stem(doc), blocks)
    if doc.file_type == 'docx':
        # An uploaded Word file: its paragraphs as extracted.
        return spec_from(_stem(doc), text_blocks(doc.content_text or ''))
    return spec_from(_stem(doc), text_blocks(_text_of(doc)))


def _markdown_of(spec: dict) -> str:
    from chat.tools.office import document

    return document.extract_text({**spec, 'subtitle': spec.get('subtitle') or ''}).strip() + '\n'


def build(doc: Document, fmt: str) -> tuple[bytes, str, str]:
    """(bytes, filename, mime) for `doc` as `fmt`, or `ExportError`."""
    fmt = (fmt or '').lower().strip('.')
    offered = formats_for(doc)
    if fmt not in offered:
        if not offered:
            raise ExportError(f'A {doc.file_type or "file"} of this kind has no export formats.')
        raise ExportError(f'{doc.name} can be exported as {", ".join(offered)}, not {fmt}.')
    name = f'{_stem(doc)}.{fmt}'

    if doc.file_type in ('docx', 'md', 'txt'):
        spec = _document_spec(doc)
        if fmt == 'pdf':
            from chat.tools.office import document, pdf

            return pdf.render(spec, _images(doc, document.image_paths(spec))), name, MIME['pdf']
        if fmt == 'docx':
            from chat.tools.office import document

            return document.render(spec, _images(doc, document.image_paths(spec))), name, MIME['docx']
        text = _markdown_of(spec)
        if fmt == 'txt':
            text = text.replace('**', '')
        return text.encode('utf-8'), name, MIME[fmt]

    if doc.file_type == 'pptx':
        spec = (doc.metadata or {}).get('spec')
        if not isinstance(spec, dict) or not spec.get('slides'):
            raise ExportError(
                'This deck was uploaded, so its slides cannot be redrawn here yet. '
                'Download the .pptx and export it from PowerPoint.', 409)
        return deck_pdf(doc, spec), name, MIME['pdf']

    if doc.file_type == 'xlsx':
        return workbook_csv(doc), name, MIME['csv']

    if doc.file_type == 'csv':
        return csv_workbook(_text_of(doc), _stem(doc)), name, MIME['xlsx']

    raise ExportError(f'{doc.name} cannot be exported.')  # pragma: no cover — FORMATS guards


# ---------------------------------------------------------------------------
# Spreadsheets
# ---------------------------------------------------------------------------

def workbook_csv(doc: Document, sheet: str | None = None) -> bytes:
    """One sheet as CSV — calculated values, not formula text.

    Cached values where the file stores them, computed
    (`inference/formulas.py`) where it does not — a workbook written by
    xlsxwriter stores formulas without their results. A formula nothing can
    evaluate falls back to its text, which is more useful than an empty cell.
    """
    import openpyxl

    from . import formulas
    from .office_edit import _read_bytes

    data = _read_bytes(doc)
    book = openpyxl.load_workbook(io.BytesIO(data))
    try:
        ws = book[sheet] if sheet and sheet in book.sheetnames else book.worksheets[0]
        out = io.StringIO()
        writer = csv.writer(out)
        for row in ws.iter_rows():
            writer.writerow([_csv_cell(book, ws.title, c) for c in row])
        return out.getvalue().encode('utf-8')
    finally:
        book.close()


def _csv_cell(book, sheet: str, cell) -> Any:
    from . import formulas

    raw = cell.value
    if isinstance(raw, str) and raw.startswith('='):
        try:
            return formulas.json_value(formulas.cell(book, sheet, cell.coordinate))
        except formulas.FormulaError:
            return raw
    if isinstance(raw, bool):
        return raw
    return raw


def _number(text: str) -> Any:
    try:
        if text.strip() and text.strip().lstrip('-').replace('.', '', 1).isdigit():
            return float(text) if '.' in text else int(text)
    except ValueError:
        pass
    return text


def csv_workbook(text: str, sheet_name: str) -> bytes:
    import xlsxwriter

    buf = io.BytesIO()
    book = xlsxwriter.Workbook(buf, {'in_memory': True, 'strings_to_formulas': False})
    ws = book.add_worksheet((sheet_name or 'Sheet1')[:31].replace('/', '-') or 'Sheet1')
    bold = book.add_format({'bold': True})
    for r, row in enumerate(csv.reader(io.StringIO(text))):
        for c, cell in enumerate(row):
            ws.write(r, c, _number(cell) if r else cell, bold if r == 0 else None)
    book.close()
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Decks → PDF: one landscape page per slide, in the deck's own colours
# ---------------------------------------------------------------------------

def deck_pdf(doc: Document, spec: dict) -> bytes:
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_LEFT
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.pdfgen import canvas as pdfcanvas
    from reportlab.platypus import Frame, Paragraph, Table, TableStyle

    from chat.tools.office.pdf import _rich

    W, H = 960, 540  # 16:9 in points
    t = spec.get('theme') or {}

    def col(key, default):
        value = str(t.get(key) or default).lstrip('#')
        try:
            return colors.HexColor('#' + value)
        except Exception:  # noqa: BLE001
            return colors.HexColor('#' + default)

    bg, surface, text_c = col('background', 'ffffff'), col('surface', 'f3f5f8'), col('text', '1a1a1a')
    muted, accent, on_accent = col('muted', '5f6368'), col('accent', '2a78d6'), col('on_accent', 'ffffff')

    def style(size, colour=text_c, bold=False, italic=False):
        font = 'Helvetica-BoldOblique' if bold and italic else 'Helvetica-Bold' if bold \
            else 'Helvetica-Oblique' if italic else 'Helvetica'
        return ParagraphStyle('s', fontName=font, fontSize=size, leading=size * 1.25,
                              textColor=colour, alignment=TA_LEFT)

    def para(c, text_value, x, y, w, h, st):
        Frame(x, y, w, h, showBoundary=0, leftPadding=0, rightPadding=0,
              topPadding=0, bottomPadding=0).addFromList([Paragraph(_rich(text_value), st)], c)

    def bullets(c, items, x, y, w, h, size=20):
        story = []
        for b in items or []:
            level = b.get('level', 0) if isinstance(b, dict) else 0
            body = b.get('text', '') if isinstance(b, dict) else str(b)
            st = style(size - 3 if level else size, muted if level else text_c)
            st.leftIndent = 24 if level else 0
            story.append(Paragraph(('– ' if level else '• ') + _rich(body), st))
        Frame(x, y, w, h, showBoundary=0, leftPadding=0, rightPadding=0,
              topPadding=0, bottomPadding=0).addFromList(story, c)

    def title_bar(c, title):
        para(c, title or '', 50, H - 110, W - 100, 70, style(30, bold=True))
        c.setFillColor(accent)
        c.rect(50, H - 118, 64, 4, stroke=0, fill=1)

    buf = io.BytesIO()
    c = pdfcanvas.Canvas(buf, pagesize=(W, H))
    c.setTitle(str(spec.get('title') or _stem(doc)))
    for n, s in enumerate(spec.get('slides') or [], 1):
        layout = s.get('layout')
        c.setFillColor(bg)
        c.rect(0, 0, W, H, stroke=0, fill=1)
        if layout in ('title', 'closing'):
            band = bool(t.get('accent_title'))
            if band:
                c.setFillColor(accent)
                c.rect(0, 0, W, H, stroke=0, fill=1)
            else:
                c.setFillColor(accent)
                c.rect(50, H * 0.35, 10, H * 0.32, stroke=0, fill=1)
            fg = on_accent if band else text_c
            para(c, s.get('title') or '', 82, H * 0.46, W - 140, 110, style(40, fg, bold=True))
            if s.get('subtitle'):
                para(c, s['subtitle'], 82, H * 0.28, W - 140, 70, style(20, on_accent if band else muted))
        elif layout == 'section':
            c.setFillColor(accent)
            c.rect(50, H * 0.6, 86, 5, stroke=0, fill=1)
            para(c, s.get('title') or '', 50, H * 0.38, W - 100, 90, style(34, bold=True))
            if s.get('subtitle'):
                para(c, s['subtitle'], 50, H * 0.26, W - 100, 50, style(18, muted))
        elif layout == 'quote':
            para(c, '“', 70, H - 170, 80, 110, style(90, accent, bold=True))
            para(c, s.get('quote') or '', 140, H * 0.32, W - 280, 200, style(26, italic=True))
            if s.get('attribution'):
                para(c, '— ' + s['attribution'], 140, H * 0.2, W - 280, 40, style(16, muted))
        else:
            title_bar(c, s.get('title'))
            top, bottom = H - 140, 60
            if layout == 'bullets':
                bullets(c, s.get('bullets'), 50, bottom, W - 100, top - bottom)
            elif layout == 'two_column':
                half = (W - 140) / 2
                for i, side in enumerate(('left', 'right')):
                    column = s.get(side) or {}
                    x = 50 + i * (half + 40)
                    if column.get('heading'):
                        para(c, column['heading'], x, top - 34, half, 34, style(20, accent, bold=True))
                    bullets(c, column.get('bullets'), x, bottom, half, top - bottom - 44, size=17)
            elif layout == 'stats':
                stats = s.get('stats') or []
                if stats:
                    gap = 24
                    w = (W - 100 - gap * (len(stats) - 1)) / len(stats)
                    for i, st in enumerate(stats):
                        x = 50 + i * (w + gap)
                        c.setFillColor(surface)
                        c.rect(x, bottom + 60, w, 200, stroke=0, fill=1)
                        c.setFillColor(accent)
                        c.rect(x, bottom + 256, w, 4, stroke=0, fill=1)
                        para(c, str(st.get('value', '')), x + 18, bottom + 150, w - 36, 70,
                             style(40, accent, bold=True))
                        para(c, str(st.get('label', '')), x + 18, bottom + 80, w - 36, 60, style(15, muted))
            elif layout in ('table', 'chart'):
                if layout == 'table':
                    columns, rows = s.get('columns') or [], s.get('rows') or []
                else:
                    chart = s.get('chart') or {}
                    xs: list[str] = []
                    for series in chart.get('series') or []:
                        for p in series.get('points') or []:
                            if p.get('x') not in xs:
                                xs.append(p.get('x'))
                    columns = [chart.get('x_label') or 'Category',
                               *[sr.get('name', '') for sr in chart.get('series') or []]]
                    rows = [[x] + [next((str(p.get('y')) for p in sr.get('points') or []
                                         if p.get('x') == x), '—')
                                   for sr in chart.get('series') or []] for x in xs]
                if columns:
                    cell = style(12)
                    head = style(12, on_accent, bold=True)
                    data = [[Paragraph(_rich(str(h)), head) for h in columns]] + \
                           [[Paragraph(_rich(str(v)), cell) for v in r] for r in rows[:14]]
                    table = Table(data, colWidths=[(W - 100) / len(columns)] * len(columns))
                    table.setStyle(TableStyle([
                        ('BACKGROUND', (0, 0), (-1, 0), accent),
                        ('ROWBACKGROUNDS', (0, 1), (-1, -1), [bg, surface]),
                        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
                    ]))
                    _, th = table.wrap(W - 100, top - bottom)
                    table.drawOn(c, 50, top - th)
                if s.get('caption'):
                    para(c, s['caption'], 50, 24, W - 100, 30, style(12, muted, italic=True))
            elif layout == 'image':
                c.setFillColor(surface)
                c.rect(50, bottom, W - 100, top - bottom - 10, stroke=0, fill=1)
                data = _images(doc, [s.get('image')]).get(s.get('image')) if s.get('image') else None
                if data:
                    from reportlab.lib.utils import ImageReader

                    img = ImageReader(io.BytesIO(data))
                    iw, ih = img.getSize()
                    box_w, box_h = W - 120, top - bottom - 30
                    scale = min(box_w / iw, box_h / ih)
                    c.drawImage(img, 50 + (W - 100 - iw * scale) / 2, bottom + 10 + (box_h - ih * scale) / 2,
                                iw * scale, ih * scale)
                if s.get('caption'):
                    para(c, s['caption'], 50, 24, W - 100, 30, style(12, muted, italic=True))
        if layout not in ('title',):
            c.setFillColor(muted)
            c.setFont('Helvetica', 10)
            c.drawRightString(W - 40, 18, str(n))
        c.showPage()
    c.save()
    return buf.getvalue()
