"""
Office files as tools: `render_deck`, `render_workbook`, `render_document`.

Each takes a **spec** — slides, sheets, blocks — and our code produces the
file. The model never writes python-pptx code and never lays anything out,
for the reason `render_chart` exists: a model asked to be a layout engine
produces a different-looking file every time, and some of them broken. One
renderer and three themes are one design system.

Three properties are shared, and they are why these tools can run without
asking in chat (unlike `write_file`):

* **They only create.** A name that is taken becomes `name (2).ext`;
  replacing needs `overwrite: true`, and even then the old file goes to the
  recycle bin rather than being written over (`vfs.write_binary`). So the
  effect is `reversible` in the autonomy ladder's sense — nothing done here is
  beyond the user's own undo.
* **They go through the caller's `FileScope`**, like every other file tool:
  `requires="files"` withholds them where there is no scope, images are read
  through the same walk, and the write is checked against the same prefix.
* **Every limit refuses rather than truncates** (`office/spec.py`), and says
  what to change.

The rendering is CPU work on pure-Python libraries, done in the web process:
bounded by those same limits, and far cheaper than a LibreOffice sidecar on a
box this size.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict

from asgiref.sync import sync_to_async

from ..charts import render_chart as _register_chart  # noqa: F401 — schema reused below
from ..registry import get as _registered
from ..registry import tool
from . import deck, document, workbook
from .spec import SpecError
from .themes import THEME_NAMES

logger = logging.getLogger(__name__)

#: A chart on a slide or in a document takes exactly `render_chart`'s shape.
_CHART_SCHEMA = {
    **_registered('render_chart').schema['function']['parameters'],
    'description': 'A chart in the same shape render_chart takes.',
}

_PATH = {
    'type': 'string',
    'description': (
        'Where to save it, e.g. "/Chat/q3-review{ext}". A bare file name is '
        'saved in your own folder.'
    ),
}
_OVERWRITE = {
    'type': 'boolean',
    'description': (
        'Replace a file already at this path (it goes to the recycle bin). '
        'Without this, a taken name is saved as "name (2)".'
    ),
}


def _path_schema(ext: str) -> dict:
    return {**_PATH, 'description': _PATH['description'].format(ext=ext)}


# ---------------------------------------------------------------------------
# Shared plumbing
# ---------------------------------------------------------------------------

def _target(scope, raw: Any, ext: str, fallback: str) -> str:
    """The path to save to: the right extension, and in a writable folder.

    A bare name goes in the scope's own write folder (`/Chat/` in chat, the
    agent's home for an agent) — the only place most scopes can write, so
    defaulting there turns "deck.pptx" into a file rather than a refusal.
    """
    from inference.vfs import safe_name

    path = str(raw or '').strip() or f'{safe_name(fallback)[:80] or "untitled"}.{ext}'
    leaf = path.replace('\\', '/').rstrip('/').rsplit('/', 1)[-1]
    if '.' in leaf:
        if leaf.rsplit('.', 1)[-1].lower() != ext:
            raise SpecError(f'This tool writes .{ext} files; the path must end in .{ext}.')
    else:
        path = f'{path.rstrip("/")}.{ext}'
    bare = '/' not in path.strip('/')
    if bare and scope.write_prefix:
        path = '/' + '/'.join(scope.write_prefix) + '/' + path.strip('/')
    return path


def _load_images(scope, paths: list[str]) -> dict[str, bytes]:
    from inference.vfs import read_image

    return {p: read_image(scope, p)[0] for p in dict.fromkeys(paths)}


async def _save(context: Dict, work) -> str:
    """Run `work(scope)` off the event loop; render its outcome for the model."""
    from inference.vfs import VfsError

    scope = context.get('file_scope')
    if scope is None:
        return json.dumps({
            'error': 'There is no file workspace here, so nothing can be saved. '
                     'An agent needs file access turned on in its settings.'
        })
    try:
        result = await sync_to_async(work)(scope)
    except (SpecError, VfsError) as exc:
        return json.dumps({'error': str(exc)})
    except Exception:
        logger.exception('[Office] render failed')
        return json.dumps({'error': 'The file could not be rendered. Simplify it and try again.'})

    if result.get('renamed'):
        result['note'] = (
            f'A file with that name already existed, so this was saved as '
            f'{result["path"]}. Pass overwrite=true to replace the old one instead.'
        )
    result['rendered'] = (
        f'Saved {result["path"]}. The user sees it as a file card they can '
        f'preview and download — do not paste its contents back; say in a '
        f'sentence or two what is in it.'
    )
    return json.dumps(result, default=str)


# ---------------------------------------------------------------------------
# render_workbook
# ---------------------------------------------------------------------------

@tool({
    'type': 'function',
    'function': {
        'name': 'render_workbook',
        'description': (
            'Create an Excel workbook (.xlsx) from sheets of typed columns and '
            'rows; the app does all formatting. Use it whenever the user wants '
            'a spreadsheet, a table they will work in, or numbers they will '
            'reuse. Keep calculations live: a value starting with "=" is a '
            'formula (row 1 is the header, data starts on row 2, and {r} means '
            '"this row", e.g. "=B{r}*C{r}"), and totals: true adds a SUM row. '
            'Do not type in totals you computed. For more than a few thousand '
            'rows, compute with execute_python first and save a summary.'
        ),
        'parameters': {
            'type': 'object',
            'properties': {
                'path': _path_schema('.xlsx'),
                'sheets': {
                    'type': 'array',
                    'description': f'At most {workbook.MAX_SHEETS} sheets.',
                    'items': {
                        'type': 'object',
                        'properties': {
                            'name': {'type': 'string', 'description': 'Sheet tab name, at most 31 characters.'},
                            'columns': {
                                'type': 'array',
                                'items': {
                                    'type': 'object',
                                    'properties': {
                                        'header': {'type': 'string'},
                                        'type': {
                                            'type': 'string',
                                            'enum': list(workbook.COLUMN_TYPES),
                                            'description': 'percent takes fractions (0.25 is 25%); date takes YYYY-MM-DD.',
                                        },
                                        'currency': {
                                            'type': 'string',
                                            'enum': list(workbook.CURRENCY_SYMBOLS),
                                            'description': 'For currency columns.',
                                        },
                                    },
                                    'required': ['header'],
                                    'additionalProperties': False,
                                },
                            },
                            'rows': {
                                'type': 'array',
                                'description': f'One list of values per row, in column order. At most {workbook.MAX_ROWS}.',
                                'items': {'type': 'array', 'items': {'type': ['string', 'number', 'boolean', 'null']}},
                            },
                            'totals': {
                                'type': 'boolean',
                                'description': 'Add a Total row summing every number/integer/currency column.',
                            },
                            'chart': {
                                'type': 'object',
                                'description': 'A native Excel chart over this sheet\'s own columns.',
                                'properties': {
                                    'kind': {'type': 'string', 'enum': ['bar', 'column', 'line', 'area', 'scatter', 'pie']},
                                    'title': {'type': 'string'},
                                    'x': {'type': 'string', 'description': 'Header of the category column.'},
                                    'y': {'type': 'array', 'items': {'type': 'string'}, 'description': 'Headers of the numeric columns to plot.'},
                                    'x_label': {'type': 'string'},
                                    'y_label': {'type': 'string'},
                                    'stacked': {'type': 'boolean'},
                                },
                                'required': ['kind', 'title', 'x', 'y'],
                                'additionalProperties': False,
                            },
                        },
                        'required': ['name', 'columns', 'rows'],
                        'additionalProperties': False,
                    },
                },
                'overwrite': _OVERWRITE,
            },
            'required': ['sheets'],
            'additionalProperties': False,
        },
    },
}, requires='files', effect='reversible')
async def render_workbook(args: Dict, context: Dict) -> str:
    try:
        spec = workbook.validate(args)
    except SpecError as exc:
        return json.dumps({'error': str(exc)})

    def work(scope):
        from inference.vfs import write_binary

        path = _target(scope, args.get('path'), 'xlsx', spec['sheets'][0]['name'])
        data, warnings = workbook.render(spec)
        result = write_binary(scope, path, data, text=workbook.extract_text(spec),
                              spec=workbook.preview(spec),
                              overwrite=bool(args.get('overwrite')))
        result['sheets'] = [{'name': s['name'], 'rows': len(s['rows'])} for s in spec['sheets']]
        if warnings:
            result['warnings'] = warnings
        return result

    return await _save(context, work)


# ---------------------------------------------------------------------------
# render_deck
# ---------------------------------------------------------------------------

_BULLETS = {
    'type': 'array',
    'items': {'type': 'string'},
    'description': (
        f'At most {deck.MAX_BULLETS}, each under {deck.BULLET_CHARS} characters. '
        'Start one with "- " to make it a sub-point. **bold** works.'
    ),
}


@tool({
    'type': 'function',
    'function': {
        'name': 'render_deck',
        'description': (
            'Create a PowerPoint deck (.pptx). You choose what each slide says '
            'and its layout; the app does all design, so never describe fonts, '
            'colours or positions. Layouts: title, section, bullets, '
            'two_column, chart (a native, editable chart), image (a picture '
            'from the user\'s files), table, quote, stats (up to 4 big '
            'numbers), closing. One idea per slide; a slide that will not fit '
            'is refused — split it rather than cramming. Put what the presenter '
            'should say in notes.'
        ),
        'parameters': {
            'type': 'object',
            'properties': {
                'path': _path_schema('.pptx'),
                'title': {'type': 'string', 'description': 'The deck\'s title.'},
                'theme': {'type': 'string', 'enum': list(THEME_NAMES),
                          'description': 'clean (default), bold, or dark.'},
                'slides': {
                    'type': 'array',
                    'description': f'At most {deck.MAX_SLIDES}.',
                    'items': {
                        'type': 'object',
                        'properties': {
                            'layout': {'type': 'string', 'enum': list(deck.LAYOUTS)},
                            'title': {'type': 'string', 'description': f'Under {deck.TITLE_CHARS} characters. Every layout but quote.'},
                            'subtitle': {'type': 'string', 'description': 'title, section, closing.'},
                            'bullets': _BULLETS,
                            'left': {
                                'type': 'object', 'description': 'two_column.',
                                'properties': {'heading': {'type': 'string'}, 'bullets': _BULLETS},
                                'required': ['bullets'], 'additionalProperties': False,
                            },
                            'right': {
                                'type': 'object', 'description': 'two_column.',
                                'properties': {'heading': {'type': 'string'}, 'bullets': _BULLETS},
                                'required': ['bullets'], 'additionalProperties': False,
                            },
                            'chart': _CHART_SCHEMA,
                            'image': {'type': 'string', 'description': 'image: path of an image in the user\'s files.'},
                            'caption': {'type': 'string', 'description': 'chart, image: one line under it.'},
                            'columns': {'type': 'array', 'items': {'type': 'string'},
                                        'description': f'table: at most {deck.TABLE_COLS}.'},
                            'rows': {'type': 'array', 'items': {'type': 'array', 'items': {'type': 'string'}},
                                     'description': f'table: at most {deck.TABLE_ROWS}.'},
                            'quote': {'type': 'string'},
                            'attribution': {'type': 'string'},
                            'stats': {
                                'type': 'array', 'description': 'stats: 1 to 4.',
                                'items': {
                                    'type': 'object',
                                    'properties': {
                                        'value': {'type': 'string', 'description': 'e.g. "₹4.2 Cr", "38%".'},
                                        'label': {'type': 'string'},
                                    },
                                    'required': ['value', 'label'],
                                    'additionalProperties': False,
                                },
                            },
                            'notes': {'type': 'string', 'description': 'Speaker notes.'},
                        },
                        'required': ['layout'],
                        'additionalProperties': False,
                    },
                },
                'overwrite': _OVERWRITE,
            },
            'required': ['slides'],
            'additionalProperties': False,
        },
    },
}, requires='files', effect='reversible')
async def render_deck(args: Dict, context: Dict) -> str:
    try:
        spec = deck.validate(args)
    except SpecError as exc:
        return json.dumps({'error': str(exc)})

    def work(scope):
        from inference.vfs import write_binary

        path = _target(scope, args.get('path'), 'pptx', spec['title'] or 'deck')
        data = deck.render(spec, _load_images(scope, deck.image_paths(spec)))
        result = write_binary(scope, path, data, text=deck.extract_text(spec),
                              spec=deck.preview(spec),
                              overwrite=bool(args.get('overwrite')))
        result['slides'] = len(spec['slides'])
        return result

    return await _save(context, work)


# ---------------------------------------------------------------------------
# render_document
# ---------------------------------------------------------------------------

@tool({
    'type': 'function',
    'function': {
        'name': 'render_document',
        'description': (
            'Create a Word document (.docx) from a title and a list of blocks: '
            'heading, paragraph, bullets, numbered, table, quote, image, chart, '
            'page_break. The app does all styling. Use it when the user wants '
            'a report, memo or letter as a Word file; for notes they will only '
            'read here, write markdown with write_file instead. **bold** and '
            '*italic* work inside text. A chart block is saved as its data '
            'table (Word files get no drawn charts).'
        ),
        'parameters': {
            'type': 'object',
            'properties': {
                'path': _path_schema('.docx'),
                'title': {'type': 'string'},
                'subtitle': {'type': 'string'},
                'theme': {'type': 'string', 'enum': list(document.DOC_THEMES)},
                'blocks': {
                    'type': 'array',
                    'description': f'At most {document.MAX_BLOCKS}.',
                    'items': {
                        'type': 'object',
                        'properties': {
                            'type': {'type': 'string', 'enum': list(document.BLOCK_TYPES)},
                            'text': {'type': 'string', 'description': 'heading, paragraph, quote.'},
                            'level': {'type': 'integer', 'enum': [1, 2, 3], 'description': 'heading.'},
                            'items': {'type': 'array', 'items': {'type': 'string'},
                                      'description': 'bullets, numbered.'},
                            'columns': {'type': 'array', 'items': {'type': 'string'}, 'description': 'table.'},
                            'rows': {'type': 'array', 'items': {'type': 'array', 'items': {'type': 'string'}},
                                     'description': 'table.'},
                            'caption': {'type': 'string', 'description': 'table, image.'},
                            'path': {'type': 'string', 'description': 'image: path of an image in the user\'s files.'},
                            'chart': _CHART_SCHEMA,
                        },
                        'required': ['type'],
                        'additionalProperties': False,
                    },
                },
                'overwrite': _OVERWRITE,
            },
            'required': ['title', 'blocks'],
            'additionalProperties': False,
        },
    },
}, requires='files', effect='reversible')
async def render_document(args: Dict, context: Dict) -> str:
    try:
        spec = document.validate(args)
    except SpecError as exc:
        return json.dumps({'error': str(exc)})

    def work(scope):
        from inference.vfs import write_binary

        path = _target(scope, args.get('path'), 'docx', spec['title'])
        data = document.render(spec, _load_images(scope, document.image_paths(spec)))
        result = write_binary(scope, path, data, text=document.extract_text(spec),
                              spec=document.preview(spec),
                              overwrite=bool(args.get('overwrite')))
        result['blocks'] = len(spec['blocks'])
        if document.chart_count(spec):
            result['charts_as_tables'] = document.chart_count(spec)
        return result

    return await _save(context, work)


OFFICE_TOOLS = ('render_deck', 'render_workbook', 'render_document')
