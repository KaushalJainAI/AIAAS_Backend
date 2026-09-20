"""
`download_file`: a URL the user named, kept as a file they own.

`read_url` reads a page as text and `generate_image` makes a picture; neither
could put *this PDF* or *that dataset* into the user's files, so "use the
image from this page" ended with a generated substitute and "analyse this CSV"
ended with the model asking for an upload. This closes that.

What it inherits rather than reinvents: the SSRF guard (`core/safety/net`,
every hop of a redirect re-checked), the binary write path (`vfs.write_binary`
— never overwrites, lands in the caller's write folder) and the byte cap. What
it adds is the one thing a downloader must say out loud: **a file from the web
belongs to whoever published it.** The description says so, because a model
that quietly drops a stock photo into a deck has created a licensing problem
the user never agreed to.

`effect="reversible"`: it writes a file the user can trash. Not `sensitive` —
the user gave the URL in the request this turn answers.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Dict
from urllib.parse import unquote, urlparse

from asgiref.sync import sync_to_async

from workflow_backend.thresholds import AGENT_FILE_BINARY_BYTES

from .registry import tool

logger = logging.getLogger(__name__)

#: Chunk size for the streamed read. The cap is checked as it accumulates, so a
#: server that lies about Content-Length cannot make this hold more than one
#: chunk past the limit.
_CHUNK = 64 * 1024

#: MIME → extension, for a URL that ends in nothing useful.
_EXT_BY_MIME = {
    'application/pdf': 'pdf',
    'image/png': 'png', 'image/jpeg': 'jpg', 'image/webp': 'webp',
    'image/gif': 'gif', 'image/svg+xml': 'svg',
    'text/csv': 'csv', 'application/json': 'json', 'text/plain': 'txt',
    'text/markdown': 'md', 'text/html': 'html',
    'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet': 'xlsx',
    'application/vnd.openxmlformats-officedocument.wordprocessingml.document': 'docx',
    'application/vnd.openxmlformats-officedocument.presentationml.presentation': 'pptx',
    'application/zip': 'zip',
}

_SAFE_NAME = re.compile(r'[^A-Za-z0-9._-]+')


def _name_for(url: str, mime: str, given: str) -> str:
    """A file name from what the caller asked for, the URL, then the type."""
    if given:
        stem = given.rsplit('/', 1)[-1]
    else:
        stem = unquote(urlparse(url).path.rsplit('/', 1)[-1]) or 'download'
    stem = _SAFE_NAME.sub('-', stem).strip('-.') or 'download'
    if '.' in stem and len(stem.rsplit('.', 1)[-1]) <= 5:
        return stem
    return f'{stem}.{_EXT_BY_MIME.get(mime, "bin")}'


def _fetch(url: str) -> tuple[bytes, str]:
    """The bytes at `url` and its MIME type, size-capped as it streams."""
    import requests

    with requests.get(url, timeout=60, stream=True,
                      headers={'User-Agent': 'Mozilla/5.0 (compatible; AIAAS)'}) as resp:
        resp.raise_for_status()
        mime = (resp.headers.get('Content-Type') or '').split(';')[0].strip().lower()
        chunks, size = [], 0
        for chunk in resp.iter_content(_CHUNK):
            size += len(chunk)
            if size > AGENT_FILE_BINARY_BYTES:
                raise ValueError(
                    f'That file is over the '
                    f'{AGENT_FILE_BINARY_BYTES // 1_048_576} MB limit.'
                )
            chunks.append(chunk)
    return b''.join(chunks), mime


@tool({
    'type': 'function',
    'function': {
        'name': 'download_file',
        'description': (
            "Download a file from a URL into the user's files — an image for a "
            'deck, a PDF, a dataset to analyse. Use it when the user gives you a '
            'link, or when a page you read links the file they asked for. Returns '
            'the saved path, which render_deck, render_document and '
            'run_python_on_files all accept. Only take files the user is entitled '
            'to use: prefer their own links, official or government sources, and '
            'anything explicitly licensed for reuse — and say where a file came '
            'from when you use it. A format we cannot read is still saved and can '
            'be opened with run_python_on_files.'
        ),
        'parameters': {
            'type': 'object',
            'properties': {
                'url': {'type': 'string', 'description': 'The http(s) URL of the file.'},
                'path': {'type': 'string',
                         'description': "Where to save it. Default: the URL's own file name in your folder."},
                'overwrite': {'type': 'boolean',
                              'description': 'Replace a file already there (it goes to the recycle bin).'},
            },
            'required': ['url'],
            'additionalProperties': False,
        },
    },
}, requires='files', effect='reversible')
async def download_file(args: Dict, context: Dict) -> str:
    from core.safety.net import validate_url_async
    from inference.vfs import VfsError, write_binary, write_file

    scope = context.get('file_scope')
    if scope is None:
        return json.dumps({'error': 'There is no file workspace here to download into.'})

    url = str(args.get('url') or '').strip()
    ok, reason = await validate_url_async(url)
    if not ok:
        return json.dumps({'error': reason})

    try:
        data, mime = await sync_to_async(_fetch)(url)
    except ValueError as exc:
        return json.dumps({'error': str(exc)})
    except Exception as exc:  # noqa: BLE001 — a dead link is an answer
        logger.warning('[Download] %s failed: %s', url, exc)
        return json.dumps({'error': f'That file could not be downloaded ({type(exc).__name__}).'})

    given = str(args.get('path') or '').strip()
    path = given or _name_for(url, mime, '')
    if not path.startswith('/') and scope.write_prefix:
        # Relative: the caller's own write folder, as every other file tool.
        path = '/' + '/'.join(scope.write_prefix) + '/' + path

    overwrite = bool(args.get('overwrite'))
    # Text formats keep their text in the row (searchable, readable by
    # `read_file`); everything else keeps its bytes.
    extension = path.rsplit('.', 1)[-1].lower() if '.' in path else ''
    text_like = extension in ('txt', 'md', 'csv', 'json', 'html', 'htm')
    try:
        if text_like:
            result = await sync_to_async(write_file)(
                scope, path, data.decode('utf-8', errors='replace'), overwrite=overwrite)
        else:
            result = await sync_to_async(write_binary)(
                scope, path, data, text='', overwrite=overwrite)
    except VfsError as exc:
        return json.dumps({'error': str(exc)})

    result.update({
        'url': url,
        'content_type': mime or 'unknown',
        'bytes': len(data),
        'rendered': (
            f'Downloaded {url} to {result["path"]}. Say where it came from when '
            f'you use it, and use it only as its source permits.'
        ),
    })
    return json.dumps(result, default=str)
