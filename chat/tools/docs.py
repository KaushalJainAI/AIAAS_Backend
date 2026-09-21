"""
Read a scanned document the way a person would: as text, as rows, or as fields.

Upload extraction already reads born-digital files; this is for the rest —
scanned PDFs and photos of bills, forms and letters. A PDF is re-read page by
page from the stored bytes (never the whole file at once); an image with no
extracted text is not guessed at — `ask_vision` holds the pixels, so the tool
says so. `fields` runs the extraction engine against a saved schema, and
low-confidence values wait on the Extraction page where they belong.
"""
from __future__ import annotations

import json
import logging
from typing import Dict

from asgiref.sync import sync_to_async

from workflow_backend.thresholds import TOOL_OUTPUT_CHAR_LIMIT

from .registry import tool

from tools_config.overlay import alimit

from tools_config.settings_schema import (
    _OCR_MAX_PAGES,
    _OCR_MAX_ROWS,
    _OCR_PAGE_CHARS,
)

logger = logging.getLogger(__name__)

#: Pages re-read from one PDF per call. A scanned bill is one page; a refusing
#: call that says "fewer pages" beats a turn spent waiting on eighty. Now a
#: workspace knob (`ocr_document.maxPages`); the constant stays as the floor
#: under a failed overlay read.
OCR_MAX_PAGES = 20
#: Characters handed back per page before the call says where it stopped. Now
#: a workspace knob (`ocr_document.pageChars`).
OCR_PAGE_CHARS = 8_000
#: Rows a table-mode read returns. Now a workspace knob (`ocr_document.maxRows`).
OCR_MAX_ROWS = 200


def _read_pdf_pages(doc, limit: int) -> list[str]:
    from pypdf import PdfReader

    if not doc.file:
        return []
    try:
        with doc.file.open('rb') as handle:
            reader = PdfReader(handle)
            return [(page.extract_text() or '') for page in reader.pages[:limit]]
    except Exception:  # noqa: BLE001
        logger.exception('[OCR] Could not re-read a PDF')
        return []


def _ocr_sync(user_id: int, scope, path: str, mode: str, pages: int,
              schema_id: int | None, page_chars: int = OCR_PAGE_CHARS,
              max_rows: int = OCR_MAX_ROWS) -> dict:
    from inference import vfs
    from inference.models import Document

    parent_parts, leaf = vfs._split_leaf(scope, path)
    folder = vfs._folder_at(scope, parent_parts)
    doc = vfs._document_in(scope, folder, leaf)
    if doc is None:
        raise ValueError(f'No such file: {vfs.render(scope, parent_parts + [leaf])}.')
    if not isinstance(doc, Document):
        doc = Document.objects.filter(id=doc.id).first()
        if doc is None:
            raise ValueError('That file is no longer available.')

    if mode == 'fields':
        if not schema_id:
            raise ValueError('`fields` needs `schema_id` — the saved schema to apply.')
        from inference.extraction import run_extraction
        from inference.models import ExtractionSchema

        schema = ExtractionSchema.objects.filter(id=schema_id, user_id=user_id).first()
        if schema is None:
            raise ValueError(
                f'No extraction schema {schema_id} belongs to this user.')
        stats = run_extraction([doc.id], schema.id, user_id)
        return {'schema': schema.name, 'documents': 1, **stats,
                'rendered': (
                    f'Extracted {stats.get("created", 0)} row(s) with '
                    f'"{schema.name}"; {stats.get("needs_review", 0)} need review '
                    'on the Extraction page.'
                )}

    if doc.file_type == 'pdf' and doc.file:
        texts = _read_pdf_pages(doc, min(max(pages, 1), OCR_MAX_PAGES))
        if not any(t.strip() for t in texts):
            raise ValueError(
                'The pages came back empty — this scan has no text layer. '
                'Use `ask_vision` on the page images instead of guessing.')
        capped, stopped = [], False
        for i, text in enumerate(texts):
            if len(text) > page_chars:
                capped.append(text[:page_chars])
                stopped = True
            else:
                capped.append(text)
        body = '\n'.join(capped)
        if mode == 'table':
            rows = [[c.strip() for c in line.split()] for line in body.splitlines()
                    if line.strip()]
            return {'path': path, 'pages': len(texts), 'rows': rows[:max_rows],
                    'truncated': stopped or len(rows) > max_rows}
        return {'path': path, 'pages': len(texts), 'text': body[:TOOL_OUTPUT_CHAR_LIMIT],
                'truncated': stopped or len(body) > TOOL_OUTPUT_CHAR_LIMIT}

    body = doc.content_text or ''
    if body.strip():
        if mode == 'table':
            rows = [[c.strip() for c in line.split(',')] for line in body.splitlines()
                    if line.strip()]
            return {'path': path, 'rows': rows[:max_rows], 'truncated': len(rows) > max_rows}
        return {'path': path, 'text': body[:TOOL_OUTPUT_CHAR_LIMIT],
                'truncated': len(body) > TOOL_OUTPUT_CHAR_LIMIT}
    if doc.file_type == 'image':
        raise ValueError(
            'This image has no extracted text. Ask `ask_vision` about it — '
            'it holds the pixels — rather than describing it from nothing.')
    raise ValueError('That file has no readable text.')


@tool({
    'type': 'function',
    'function': {
        'name': 'ocr_document',
        'description': (
            'Read a scanned PDF or a photo of a document — bills, forms, '
            'letters. `text` reads it, `table` returns rows, `fields` pulls '
            'structured values through a saved extraction schema (pass its '
            '`schema_id`; low-confidence values wait on the Extraction page). '
            'For an image with no text layer, ask the vision witness instead.'
        ),
        'parameters': {
            'type': 'object',
            'properties': {
                'path': {'type': 'string', 'description': 'The file in your workspace.'},
                'as': {'type': 'string', 'enum': ['text', 'table', 'fields'],
                       'description': 'How to read it. Default text.'},
                'pages': {'type': 'integer',
                          'description': 'PDF pages to re-read (default 20, fewer is faster).'},
                'schema_id': {'type': 'integer',
                              'description': 'Saved schema id, for `fields`.'},
            },
            'required': ['path'],
            'additionalProperties': False,
        },
    },
}, requires='files', effect='read')
async def ocr_document(args: Dict, context: Dict) -> str:
    scope = context.get('file_scope')
    user_id = context.get('user_id')
    if scope is None or not user_id:
        return json.dumps({'error': 'This agent has no file access, so it cannot read documents.'})
    path = str(args.get('path') or '').strip()
    if not path:
        return json.dumps({'error': 'Give the path of the document to read.'})
    mode = str(args.get('as') or 'text').strip().lower()
    if mode not in ('text', 'table', 'fields'):
        return json.dumps({'error': "`as` must be text, table or fields."})
    try:
        pages = int(args.get('pages') or await alimit(context, 'ocr_document', 'maxPages'))
    except (TypeError, ValueError):
        return json.dumps({'error': '`pages` must be a number.'})
    schema_id = args.get('schema_id')
    try:
        schema_id = int(schema_id) if schema_id is not None else None
    except (TypeError, ValueError):
        return json.dumps({'error': '`schema_id` must be the numeric id of a saved schema.'})
    try:
        out = await sync_to_async(_ocr_sync)(
            user_id, scope, path, mode, pages, schema_id,
            await alimit(context, 'ocr_document', 'pageChars'),
            await alimit(context, 'ocr_document', 'maxRows'))
    except ValueError as exc:
        return json.dumps({'error': str(exc)})
    except Exception:
        logger.exception('[OCR] ocr_document failed')
        return json.dumps({'error': 'The document could not be read.'})
    return json.dumps(out, default=str)
