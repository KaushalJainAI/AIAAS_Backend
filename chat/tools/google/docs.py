"""
Google Docs, over `docs/v1`.

Reading a Doc through Drive export gives plain text; this module reads the
document itself, so headings, lists and tables survive as structure instead
of arriving as undifferentiated prose. Creating and appending go through
`documents.create` and `batchUpdate` — both need the `documents` scope, which
is what the `google-docs` card asks for (see `lib/googleScopes.ts`; the
frontend reserved that entry for exactly this module).

Two rules. A clipped body says so and names the omission — a truncated read
and a short one must not look alike. And an edit names what it did (appended
N characters at the end) rather than pasting the document back.
"""
from __future__ import annotations

import json
from typing import Any, Dict

from tools_config.overlay import alimit

from ..registry import tool
from .client import GoogleAPIError, get_json, handles_google_errors, send_json

CONNECTOR = "google-docs"
API = "https://docs.googleapis.com/v1/documents"


def _para_text(paragraph: dict) -> str:
    return ''.join(
        str((el.get('textRun') or {}).get('content') or '')
        for el in paragraph.get('elements') or []
        if isinstance(el, dict)
    )


def _walk(structural: list) -> list[str]:
    """Structural elements into markdown-ish lines."""
    lines = []
    for el in structural or []:
        if not isinstance(el, dict):
            continue
        if 'paragraph' in el:
            para = el['paragraph'] or {}
            style = (para.get('paragraphStyle') or {}).get('namedStyleType') or ''
            text = _para_text(para).rstrip('\n')
            if not text.strip():
                continue
            if style.startswith('HEADING_'):
                try:
                    level = int(style.rsplit('_', 1)[-1])
                except ValueError:
                    level = 1
                lines.append(f"{'#' * min(level, 6)} {text.strip()}")
            elif 'LIST' in style or 'bullet' in str(para.get('bullet') or ''):
                lines.append(f'- {text.strip()}')
            else:
                lines.append(text)
        elif 'table' in el:
            for row in (el['table'] or {}).get('tableRows') or []:
                cells = []
                for cell in row.get('tableCells') or []:
                    content = cell.get('content') or []
                    cells.append(' '.join(_walk(content)).strip())
                lines.append(' | '.join(cells))
    return lines


@tool({
    'type': 'function',
    'function': {
        'name': 'docs_read',
        'description': (
            '[Google Docs] Read a document with its structure: headings, '
            'lists and tables. Takes a document id (from a Drive listing or '
            'URL). Long documents are clipped with a notice, never silently.'
        ),
        'parameters': {
            'type': 'object',
            'properties': {
                'document_id': {'type': 'string', 'description': 'The document id.'},
            },
            'required': ['document_id'],
            'additionalProperties': False,
        },
    },
}, parallel=True, effect='read', connector=CONNECTOR)
@handles_google_errors
async def docs_read(args: Dict, context: Dict) -> str:
    document_id = str(args.get('document_id') or '').strip()
    if not document_id:
        raise GoogleAPIError('tool_error', 'Give the document id.')
    cap = await alimit(context, 'docs_read', 'charLimit')
    doc = await get_json(context, f'{API}/{document_id}')
    lines = _walk((doc.get('body') or {}).get('content'))
    text = '\n'.join(lines)
    truncated = False
    if len(text) > cap:
        text = text[:cap] + '\n[... clipped: the document continues in Docs]'
        truncated = True
    return json.dumps({
        'type': 'docs_document', 'id': doc.get('documentId'),
        'title': doc.get('title'), 'text': text,
        **({'truncated': True} if truncated else {}),
    })


@tool({
    'type': 'function',
    'function': {
        'name': 'docs_create',
        'description': (
            '[Google Docs] Create a new document with a title. The user '
            'approves each document. Returns the id and URL.'
        ),
        'parameters': {
            'type': 'object',
            'properties': {
                'title': {'type': 'string', 'description': 'The document title.'},
            },
            'required': ['title'],
            'additionalProperties': False,
        },
    },
}, sensitive=True, effect='irreversible', connector=CONNECTOR)
@handles_google_errors
async def docs_create(args: Dict, context: Dict) -> str:
    title = str(args.get('title') or '').strip()
    if not title:
        raise GoogleAPIError('tool_error', 'Give the document a title.')
    made: dict[str, Any] = await send_json(
        context, 'POST', API, json={'title': title})
    doc_id = made.get('documentId') or ''
    return json.dumps({
        'type': 'docs_document_created', 'id': doc_id,
        'title': made.get('title'),
        'url': f'https://docs.google.com/document/d/{doc_id}/edit' if doc_id else '',
    })


@tool({
    'type': 'function',
    'function': {
        'name': 'docs_append',
        'description': (
            '[Google Docs] Append text to the end of a document. Paragraphs '
            'are split on blank lines. The user approves each edit.'
        ),
        'parameters': {
            'type': 'object',
            'properties': {
                'document_id': {'type': 'string', 'description': 'The document id.'},
                'text': {'type': 'string', 'description': 'Text to append.'},
            },
            'required': ['document_id', 'text'],
            'additionalProperties': False,
        },
    },
}, sensitive=True, effect='irreversible', connector=CONNECTOR)
@handles_google_errors
async def docs_append(args: Dict, context: Dict) -> str:
    document_id = str(args.get('document_id') or '').strip()
    text = str(args.get('text') or '')
    if not document_id or not text.strip():
        raise GoogleAPIError('tool_error', 'Give the document id and the text.')
    doc = await get_json(context, f'{API}/{document_id}',
                         params={'fields': 'body(content(endIndex))'})
    content = (doc.get('body') or {}).get('content') or []
    end = max([el.get('endIndex') or 1 for el in content if isinstance(el, dict)]
              + [1])
    paras = [p for p in text.split('\n\n') if p.strip()]
    body_text = '\n\n'.join(paras[:20])
    if len(paras) > 20:
        body_text += '\n\n[... 20 paragraphs appended; the rest refused: split it]'
    await send_json(context, 'POST', f'{API}/{document_id}:batchUpdate', json={
        'requests': [{'insertText': {
            'location': {'index': max(end - 1, 1)},
            'text': '\n' + body_text}}],
    })
    return json.dumps({
        'type': 'docs_appended', 'id': document_id,
        'characters': len(body_text),
        'rendered': f'Appended {len(body_text):,} characters to the document.',
    })
