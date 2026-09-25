"""
Imports: uploaded Word and PowerPoint files as editable specs.

An uploaded `.docx` / `.pptx` has no `metadata.spec`, so the apps open it
read-only and the agent tools cannot touch it. These readers convert the file
into the same spec the renderers take — best effort, and labelled as a
conversion — so afterwards nothing distinguishes an import from a file made
here.

Two rules carry it. Imports **truncate, never refuse**: a model hitting a cap
gets a refusal it can act on, but a person opening their own 60-slide deck
must not be told their file is wrong — overlong text is cut and the response
says so. And the original upload is kept as version 1: whatever the import
loses (layouts the spec cannot express, SmartArt, tracked changes) stays one
restore away.
"""
from __future__ import annotations

from typing import Any


def docx_to_spec(data: bytes, title: str) -> tuple[dict, dict[str, bytes], list[str]]:
    """An uploaded Word file as a document spec.

    Returns (spec, images, warnings). Images are keyed by suggested file name;
    the caller saves them beside the document and rewrites the spec's image
    paths to where they landed.
    """
    from docx import Document as DocxDocument
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    from chat.tools.office.document import DOC_THEMES

    import io

    try:
        doc = DocxDocument(io.BytesIO(data))
    except Exception as exc:  # noqa: BLE001 — an unreadable file is an answer, not a crash
        raise ImportError_(f'This Word file could not be read ({exc}).')

    blocks: list[dict] = []
    images: dict[str, bytes] = {}
    warnings: list[str] = []
    pending: dict | None = None  # open list block consecutive items join

    def flush():
        nonlocal pending
        if pending and pending.get('items'):
            blocks.append(pending)
        pending = None

    def push(block: dict):
        nonlocal pending
        if block['type'] in ('bullets', 'numbered'):
            if pending is None or pending['type'] != block['type']:
                flush()
                pending = {**block, 'items': []}
            pending['items'].extend(block['items'])
            if len(pending['items']) > 50:
                del pending['items'][50:]
                warnings.append('A long list was cut to 50 items.')
        else:
            flush()
            blocks.append(block)

    image_counter = 0

    def liters_to_text(paragraph) -> tuple[str, list[dict] | None]:
        """A paragraph's runs (and whether any run carries styling)."""
        runs = []
        for run in paragraph.runs:
            if not run.text:
                continue
            entry: dict[str, Any] = {'text': run.text[:6000]}
            if run.bold:
                entry['bold'] = True
            if run.italic:
                entry['italic'] = True
            font = run.font
            if font.underline:
                entry['underline'] = True
            if font.strike:
                entry['strike'] = True
            link = _run_link(paragraph, run)
            if link:
                entry['link'] = link[:500]
            runs.append(entry)
        styled = any(k in r for r in runs for k in ('bold', 'italic', 'underline', 'strike', 'link'))
        return ''.join(r['text'] for r in runs), (runs if styled else None)

    def add_table(table) -> None:
        rows = [[c.text.strip()[:500] for c in row.cells] for row in table.rows]
        rows = [r for r in rows if any(r)]
        if not rows:
            return
        width = min(max(len(r) for r in rows), 12)
        if max(len(r) for r in rows) > 12:
            warnings.append('A wide table was cut to 12 columns.')
        columns = [(rows[0][i] if i < len(rows[0]) else '') or f'Column {i + 1}'
                   for i in range(width)]
        push({'type': 'table', 'columns': columns,
              'rows': [((r + [''] * width)[:width]) for r in rows[1:]][:200],
              'caption': ''})

    def body_items():
        """Paragraphs and tables in document order. `doc.paragraphs` and
        `doc.tables` are two separate lists, and walking them one after the
        other moved every table to the end of the document."""
        for child in doc.element.body.iterchildren():
            tag = child.tag.rsplit('}', 1)[-1]
            if tag == 'p':
                yield Paragraph(child, doc)
            elif tag == 'tbl':
                yield Table(child, doc)

    for para in body_items():
        if isinstance(para, Table):
            add_table(para)
            continue
        style = para.style.name if para.style is not None else 'Normal'
        text, runs = liters_to_text(para)
        if not text.strip() and not _para_images(para):
            continue
        for blob, ext, descr in _para_images(para):
            image_counter += 1
            name = f'image-{image_counter}.{ext}'
            images[name] = blob
            push({'type': 'image', 'path': name, 'caption': (descr or '')[:300]})
        if not text.strip():
            continue
        if style == 'Title' and not any(b.get('type') == 'heading' for b in blocks):
            title = text.strip()[:200]
            continue
        if style.startswith('Heading'):
            try:
                level = min(int(style.rsplit(' ', 1)[-1]), 3)
            except ValueError:
                level = 1
            push({'type': 'heading', 'level': level, **_words(text, runs)})
        elif style in ('List Bullet', 'List Bullet 2', 'List Bullet 3'):
            push({'type': 'bullets', 'items': [text.strip()[:1000]]})
        elif style in ('List Number', 'List Number 2', 'List Number 3'):
            push({'type': 'numbered', 'items': [text.strip()[:1000]]})
        elif style in ('Quote', 'Intense Quote', 'IntenseQuote'):
            push({'type': 'quote', **_words(text, runs)})
        else:
            push({'type': 'paragraph', **_words(text, runs)})
    flush()

    if len(blocks) > 300:
        del blocks[300:]
        warnings.append('Only the first 300 blocks were imported.')
    if not blocks:
        blocks = [{'type': 'paragraph', 'text': ' '}]

    first = blocks[0]
    doc_title = title
    if first.get('type') == 'heading' and first.get('level') == 1:
        doc_title = _plain(first)
        blocks = blocks[1:] or [{'type': 'paragraph', 'text': ' '}]
    return ({'title': (doc_title or title)[:200], 'subtitle': '',
             'theme': DOC_THEMES[0], 'blocks': blocks}, images, warnings)


def _words(text: str, runs: list[dict] | None) -> dict:
    if runs is not None:
        return {'runs': runs}
    return {'text': text[:6000]}


def _plain(block: dict) -> str:
    if block.get('runs') is not None:
        return ''.join(r.get('text', '') for r in block['runs'])
    return block.get('text', '')


def _run_link(paragraph, run) -> str | None:
    """The hyperlink target wrapping a run, if any."""
    element = run._r
    parent = element.getparent()
    while parent is not None and parent is not paragraph._p:
        if parent.tag.endswith('}hyperlink'):
            r_id = parent.get('{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id')
            if r_id:
                try:
                    return paragraph.part.rels[r_id].target_ref
                except KeyError:
                    return None
        parent = parent.getparent()
    return None


def _para_images(paragraph) -> list[tuple[bytes, str, str]]:
    """Inline images as (bytes, extension, description)."""
    out = []
    for run in paragraph.runs:
        for drawing in run._r.findall('.//{http://schemas.openxmlformats.org/drawingml/2006/main}blip'):
            r_id = drawing.get('{http://schemas.openxmlformats.org/officeDocument/2006/relationships}embed')
            if not r_id:
                continue
            try:
                part = paragraph.part.rels[r_id].target_part
            except KeyError:
                continue
            blob = part.blob
            content_type = part.content_type
            ext = {'image/png': 'png', 'image/jpeg': 'jpg', 'image/gif': 'gif',
                   'image/bmp': 'bmp', 'image/webp': 'webp'}.get(content_type, 'png')
            descr = ''
            parent = run._r
            for elem in parent.iter():
                if elem.tag.endswith('}cNvPr'):
                    descr = elem.get('descr') or elem.get('name') or ''
                    break
            out.append((blob, ext, descr))
    return out


# ---------------------------------------------------------------------------
# PowerPoint
# ---------------------------------------------------------------------------

def pptx_to_spec(data: bytes, title: str) -> tuple[dict, dict[str, bytes], list[str]]:
    """An uploaded deck as a deck spec, each slide mapped to the nearest of
    our layouts. Returns (spec, images, warnings) like `docx_to_spec`."""
    import io

    from pptx import Presentation

    try:
        prs = Presentation(io.BytesIO(data))
    except Exception as exc:  # noqa: BLE001
        raise ImportError_(f'This presentation could not be read ({exc}).')

    slides: list[dict] = []
    images: dict[str, bytes] = {}
    warnings: list[str] = []
    image_counter = 0
    first_title = ''  # the first slide's own title, if it has one

    def stash(blob: bytes, ext: str) -> str:
        nonlocal image_counter
        image_counter += 1
        name = f'slide-image-{image_counter}.{ext}'
        images[name] = blob
        return name

    for slide in prs.slides:
        # Not `title`: that is the file name, the deck's fallback title.
        slide_title = ''
        slide_subtitle = ''
        bodies: list[list[tuple[str, int]]] = []
        tables: list[list[list[str]]] = []
        pictures: list[str] = []
        chart_spec: dict | None = None
        notes = ''
        if slide.has_notes_slide:
            try:
                notes = slide.notes_slide.notes_text_frame.text.strip()[:3000]
            except Exception:  # noqa: BLE001 — notes are a bonus, not the slide
                notes = ''
        for shape in slide.shapes:
            if shape.shape_type == 13:  # picture
                try:
                    blob = shape.image.blob
                    ext = shape.image.ext
                except Exception:  # noqa: BLE001 — an unreadable picture is skipped, not fatal
                    warnings.append('A picture could not be read and was skipped.')
                    continue
                pictures.append(stash(blob, ext if ext in ('png', 'jpg', 'gif', 'bmp') else 'png'))
            elif getattr(shape, 'has_table', False) and shape.has_table:
                tables.append([[cell.text.strip()[:60] for cell in row.cells]
                               for row in shape.table.rows])
            elif getattr(shape, 'has_chart', False) and shape.has_chart:
                chart_spec = _chart_of(shape, warnings)
            elif shape.has_text_frame:
                paras = [(p.text.strip(), p.level) for p in shape.text_frame.paragraphs]
                paras = [(t, l) for t, l in paras if t]
                if not paras:
                    continue
                role = _placeholder_role(shape)
                if role == 'title' and not slide_title:
                    slide_title = ' '.join(t for t, _ in paras)[:90]
                elif role == 'subtitle' and not slide_subtitle:
                    slide_subtitle = ' '.join(t for t, _ in paras)[:200]
                else:
                    bodies.append(paras)
        if not slides and slide_title:
            first_title = slide_title
        slides.append(_slide_of(slide_title, slide_subtitle, bodies, tables, pictures,
                                chart_spec, notes, warnings))
        if len(slides) >= 40:
            warnings.append('Only the first 40 slides were imported.')
            break

    if not slides:
        slides = [{'layout': 'title', 'title': title[:90] or 'Untitled', 'subtitle': ''}]
    # The first slide's real title, else the file name — never a layout's
    # placeholder text for an untitled slide.
    deck_title = first_title or title
    return ({'title': (deck_title or 'Untitled')[:90], 'theme': 'clean',
             'slides': slides}, images, warnings)


def _placeholder_role(shape) -> str:
    """title, subtitle or body, from the placeholder type (or body)."""
    if not getattr(shape, 'is_placeholder', False):
        return 'body'
    try:
        kind = shape.placeholder_format.type
    except Exception:  # noqa: BLE001 — an unreadable placeholder is content
        return 'body'
    name = getattr(kind, 'name', '')
    if name in ('TITLE', 'CENTER_TITLE', 'VERTICAL_TITLE'):
        return 'title'
    if name == 'SUBTITLE':
        return 'subtitle'
    return 'body'


def _slide_of(title, subtitle, bodies, tables, pictures, chart_spec, notes, warnings) -> dict:
    """One slide's shapes to the nearest layout."""

    def bullets(paras, limit=6):
        out = []
        for text_value, level in paras:
            out.append({'text': text_value[:160], 'level': min(level, 1)})
            if len(out) >= limit:
                break
        return out

    if chart_spec is not None:
        return {'layout': 'chart', 'title': title or 'Chart',
                'chart': chart_spec, 'notes': notes}
    if tables:
        columns = (tables[0][0] + [''] * 6)[:6] if tables[0] else ['']
        rows = [(r + [''] * len(columns))[:len(columns)] for r in tables[0][1:11]]
        return {'layout': 'table', 'title': title or 'Table',
                'columns': columns or [''], 'rows': rows or [['']], 'notes': notes}
    if pictures and not bodies and not subtitle:
        return {'layout': 'image', 'title': title or 'Image',
                'image': pictures[0], 'caption': '', 'notes': notes}
    if len(bodies) >= 2:
        left = {'heading': '', 'bullets': bullets(bodies[0], 5)}
        right = {'heading': '', 'bullets': bullets(bodies[1], 5)}
        if len(bodies) > 2:
            warnings.append('A slide with more than two columns kept the first two.')
        return {'layout': 'two_column', 'title': title or 'Compare',
                'left': left, 'right': right, 'notes': notes}
    if bodies:
        return {'layout': 'bullets', 'title': title or 'Slide',
                'bullets': bullets(bodies[0]) or [{'text': title or 'Slide', 'level': 0}],
                'notes': notes}
    if pictures:
        return {'layout': 'image', 'title': title or 'Image',
                'image': pictures[0], 'caption': '', 'notes': notes}
    if subtitle and not title:
        return {'layout': 'section', 'title': subtitle, 'subtitle': '', 'notes': notes}
    return {'layout': 'title', 'title': title or 'Untitled',
            'subtitle': subtitle, 'notes': notes}


def _chart_of(shape, warnings) -> dict | None:
    """A native chart as our chart spec, or None when it does not map."""
    try:
        chart = shape.chart
        plot = chart.plots[0]
        try:
            categories = [str(c) for c in plot.categories]
        except Exception:  # noqa: BLE001 — XY charts have no categories
            categories = []
        series = []
        for s in plot.series:
            try:
                values = list(s.values)
            except Exception:  # noqa: BLE001
                continue
            points = [{'x': categories[i] if i < len(categories) else str(i),
                       'y': v} for i, v in enumerate(values)
                      if isinstance(v, (int, float))]
            if points:
                series.append({'name': s.name or 'Series', 'points': points})
        if not series:
            return None
        title = chart.chart_title.text_frame.text if chart.has_title else 'Chart'
        return {'kind': 'column', 'title': title[:120], 'x_label': '',
                'series': series[:8]}
    except Exception:  # noqa: BLE001 — an exotic chart becomes a table, not an error
        warnings.append('A chart could not be converted and was kept as data.')
        return None


class ImportError_(Exception):
    """An upload that cannot be imported at all."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def import_upload(doc) -> dict:
    """Convert an uploaded Word/PowerPoint file into an editable one.

    Validates the converted spec, keeps the original upload as version 1
    (whatever the import loses stays one restore away), saves extracted
    images beside the document, and re-renders the real file from the spec.
    Returns {'converted': True, 'warnings': [...]}.
    """
    from chat.tools.office import deck, document
    from chat.tools.office.spec import SpecError

    from . import office_edit, versions

    stored = (doc.metadata or {}).get('spec')
    if doc.file_type not in ('docx', 'pptx') or isinstance(stored, dict):
        raise ImportError_('Only an uploaded Word or PowerPoint file needs converting — '
                           'this one is already editable here.')
    data = office_edit._read_bytes(doc)
    stem = doc.name.rpartition('.')[0] or doc.name
    if doc.file_type == 'docx':
        raw, images, warnings = docx_to_spec(data, stem)
        try:
            spec = document.validate({'title': raw['title'], 'subtitle': raw.get('subtitle', ''),
                                      'theme': raw.get('theme', 'clean'), 'blocks': raw['blocks']})
        except SpecError as exc:
            raise ImportError_(f'The conversion produced a document this editor cannot hold ({exc}).') from exc
    else:
        raw, images, warnings = pptx_to_spec(data, stem)
        try:
            spec = deck.validate({'title': raw['title'], 'theme': raw.get('theme', 'clean'),
                                  'slides': raw['slides']})
        except SpecError as exc:
            raise ImportError_(f'The conversion produced a deck this editor cannot hold ({exc}).') from exc

    # The original upload is version 1: converting loses whatever the spec
    # cannot express, and that stays restorable.
    versions.snapshot(doc, 'app')

    placed = _place_images(doc, images, warnings)

    if doc.file_type == 'docx':
        for block in spec.get('blocks') or []:
            if block.get('type') == 'image':
                block['path'] = placed.get(block.get('path'), block.get('path'))
    else:
        for slide in spec.get('slides') or []:
            if slide.get('layout') == 'image' and slide.get('image') in placed:
                slide['image'] = placed[slide['image']]

    blobs = {final: images[suggested] for suggested, final in placed.items()}
    try:
        if doc.file_type == 'docx':
            rendered = document.render(spec, blobs)
            text, preview = document.extract_text(spec), document.preview(spec)
        else:
            rendered = deck.render(spec, blobs)
            text, preview = deck.extract_text(spec), deck.preview(spec)
    except Exception as exc:  # noqa: BLE001 — a renderer bug must not eat the upload
        raise ImportError_(f'The converted file could not be drawn ({exc}).', 500) from exc
    office_edit.replace_bytes(doc, rendered, text=text, spec=preview, version_source=None)
    # `replace_bytes` clears draft state like every other real overwrite; the
    # snapshot above is the version this conversion keeps.
    return {'converted': True, 'warnings': warnings}


def _place_images(doc, images: dict[str, bytes], warnings: list[str]) -> dict[str, str]:
    """Save extracted images beside the document. Returns suggested -> path."""
    from . import filesystem as fs
    from . import vfs as vfs_mod

    if not images:
        return {}
    scope = vfs_mod.build_scope(doc.user, 'full')
    folder = fs.name_path(doc.folder)
    placed: dict[str, str] = {}
    for suggested, blob in images.items():
        path = f'{folder}/{suggested}' if folder != '/' else f'/{suggested}'
        try:
            placed[suggested] = vfs_mod.write_binary(scope, path, blob)['path']
        except Exception as exc:  # noqa: BLE001 — one bad image must not fail the import
            warnings.append(f'An image could not be kept ({exc}).')
    return placed



