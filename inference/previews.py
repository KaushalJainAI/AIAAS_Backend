"""
What a file shows when the browser cannot show the file itself.

Most browsers cannot display TIFF (or HEIC without help), so those images are
converted to PNG on the server with Pillow (already installed); the download
stays the original. Zip archives list what is inside without serving any of
it — a listing is safe to show, an entry's bytes are not ours to hand out.
Both are readable by whoever can read the file, and both are in `docs/API.md`.
"""
from __future__ import annotations

import io
import logging
import zipfile

logger = logging.getLogger(__name__)

#: Converted images are capped at this long edge: a preview, not a download.
PREVIEW_LONG_EDGE = 2400
#: Entries listed from one archive; the response says so when capped.
ARCHIVE_LIST_LIMIT = 500


class PreviewError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def preview_image(doc) -> tuple[bytes, str]:
    """PNG bytes for an image most browsers cannot display (TIFF, BMP, HEIC).

    Cached on the row's metadata (`preview_image` size stamp): the same file
    previews identically until it is overwritten, and overwrites clear the
    cache through the ordinary metadata write. Anything Pillow cannot open —
    including HEIC without `pillow-heif` — is a 400 with the sentence, never
    a crash.
    """
    from PIL import Image

    from .office_edit import _read_bytes

    name = doc.name or ''
    ext = name.rsplit('.', 1)[-1].lower() if '.' in name else ''
    if ext not in ('tif', 'tiff', 'bmp', 'heic', 'heif'):
        raise PreviewError(f'.{ext or "?"} images display in the browser already.')
    try:
        data = _read_bytes(doc)
    except Exception as exc:  # noqa: BLE001 — storage problems read as failures
        raise PreviewError('This image could not be read.') from exc
    cached = (doc.metadata or {}).get('preview_image') or {}
    if (cached.get('size') == doc.file_size and isinstance(cached.get('path'), str)
            and cached.get('path')):
        try:
            with doc.file.storage.open(cached['path'], 'rb') as fh:
                return fh.read(), 'image/png'
        except Exception:  # noqa: BLE001 — a lost cache file re-renders below
            logger.warning('Lost preview cache for document %s', doc.pk)
    try:
        with Image.open(io.BytesIO(data)) as img:
            img = img.convert('RGB')
            img.thumbnail((PREVIEW_LONG_EDGE, PREVIEW_LONG_EDGE), Image.LANCZOS)
            buf = io.BytesIO()
            img.save(buf, format='PNG')
            rendered = buf.getvalue()
    except Exception as exc:  # noqa: BLE001 — e.g. HEIC without pillow-heif
        raise PreviewError(
            'This image cannot be shown in the browser. Download it to view it.') from exc
    try:
        from django.core.files.base import ContentFile

        path = f'users/{doc.user_id}/previews/{doc.id}.png'
        saved = doc.file.storage.save(path, ContentFile(rendered))
        metadata = dict(doc.metadata or {})
        metadata['preview_image'] = {'path': saved, 'size': doc.file_size}
        type(doc).objects.filter(id=doc.id).update(metadata=metadata)
    except Exception:  # noqa: BLE001 — caching must never fail the preview
        logger.warning('Could not cache the preview of document %s', doc.pk)
    return rendered, 'image/png'


def archive_listing(doc) -> dict:
    """The files inside a zip: name, size and date each, never the bytes.

    Capped at `ARCHIVE_LIST_LIMIT` entries with `truncated: true` when cut —
    a truncated list and a complete one must not look alike. Zip bombs are
    refused at upload already (`zip_within_budget`); this re-checks before
    reading because a row can predate the gate.
    """
    from .office_edit import _read_bytes
    from .utils import zip_within_budget

    name = doc.name or ''
    ext = name.rsplit('.', 1)[-1].lower() if '.' in name else ''
    if ext != 'zip':
        raise PreviewError(f'.{ext or "?"} is not an archive.')
    try:
        data = _read_bytes(doc)
    except Exception as exc:  # noqa: BLE001
        raise PreviewError('This archive could not be read.') from exc
    if not zip_within_budget(io.BytesIO(data)):
        raise PreviewError('That archive expands to more than the budget allows.')
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            infos = zf.infolist()
    except zipfile.BadZipFile as exc:
        raise PreviewError('That file is not a readable zip archive.') from exc
    entries = []
    for info in infos:
        if info.is_dir():
            continue
        entries.append({
            'name': info.filename,
            'size': info.file_size,
            'modified': '%04d-%02d-%02dT%02d:%02d:%02d' % info.date_time,
        })
        if len(entries) >= ARCHIVE_LIST_LIMIT:
            break
    return {
        'entries': entries,
        'truncated': len([i for i in infos if not i.is_dir()]) > len(entries),
        'count': len([i for i in infos if not i.is_dir()]),
    }


def preview_kind(doc) -> str | None:
    """The Phase F preview kind for a file, or None when nothing special
    applies. Mirrors `lib/filePreview.ts::kindOf` on the frontend."""
    name = (doc.name or '').lower()
    ext = name.rsplit('.', 1)[-1] if '.' in name else ''
    if ext in ('tif', 'tiff', 'bmp', 'heic', 'heif'):
        return 'converted_image'
    if ext in ('doc', 'xls', 'ppt'):
        return 'legacy_office'
    if ext == 'pdf':
        return 'pdf'
    if ext == 'zip':
        return 'archive'
    if ext == 'eml':
        return 'email'
    if ext in ('odt', 'ods', 'odp'):
        return 'opendocument'
    return None


def legacy_sentence(doc) -> str:
    """What the preview says for an old Office binary."""
    name = (doc.name or '').lower()
    ext = name.rsplit('.', 1)[-1] if '.' in name else ''
    label = {'doc': '.docx', 'xls': '.xlsx', 'ppt': '.pptx'}.get(ext, '')
    word = {'doc': 'Word 97–2003', 'xls': 'Excel 97–2003', 'ppt': 'PowerPoint 97–2003'}.get(ext, 'old Office')
    return (f'Old Office format ({word}). Download to open it'
            + (f', or re-save as {label} to edit it here.' if label else '.'))
