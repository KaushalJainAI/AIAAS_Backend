import csv
import json
import logging
import os
import uuid
import zipfile
from xml.etree import ElementTree

import bleach
import magic
from django.core.exceptions import ValidationError
from pypdf import PdfReader

from workflow_backend.thresholds import MAX_DOCUMENT_SIZE, XLSX_EXTRACT_ROWS

logger = logging.getLogger(__name__)


def user_document_path(instance, filename: str) -> str:
    """Where a `Document`'s bytes go: ``users/<user_id>/<uuid><ext>``.

    Every segment is server-derived — the owner's id and a fresh uuid — so the
    physical layout carries no user-controlled path component at all. That is
    defence in depth beneath the logical tree: even a bug in the folder
    ownership checks could not cause a write outside the owner's directory,
    because the caller never gets to influence this string.

    The folder tree is deliberately **not** mirrored on disk. A move is a column
    write; making it a file operation would put a rename in the path of every
    drag-and-drop and give a half-failed move two disagreeing sources of truth.

    Files uploaded before this existed keep the flat names already stored in
    their `FileField`, so nothing has to be moved.
    """
    ext = os.path.splitext(filename or '')[1][:12].lower()
    return f'users/{instance.user_id}/{uuid.uuid4().hex}{ext}'


#: Filename extension → `Document.FILE_TYPE_CHOICES` value. The choices list
#: is the vocabulary the rest of the app branches on — `extract_text_from_file`
#: skips binaries by it, and `inference/extraction.py` decides whether to hand
#: a document to the model as pixels by it. Two producers used to derive it
#: independently (the upload view took the raw extension, chat attachments used
#: their own five-value set), so an uploaded PNG arrived as `file_type='png'`,
#: matched neither guard, and was read as UTF-8 with errors ignored: binary
#: noise, chunked and embedded into the index, and invisible to vision.
_EXTENSION_TYPES = {
    'pdf': 'pdf',
    'txt': 'txt', 'text': 'txt', 'log': 'txt',
    'md': 'md', 'markdown': 'md',
    'docx': 'docx', 'doc': 'docx',
    'xlsx': 'xlsx', 'xlsm': 'xlsx', 'xls': 'xlsx',
    'pptx': 'pptx', 'ppt': 'pptx',
    'csv': 'csv', 'tsv': 'csv',
    'json': 'json',
    'html': 'html', 'htm': 'html',
    'png': 'image', 'jpg': 'image', 'jpeg': 'image', 'webp': 'image',
    'gif': 'image', 'bmp': 'image', 'tiff': 'image', 'tif': 'image',
    'svg': 'image', 'image': 'image',
    'mp4': 'video', 'mov': 'video', 'webm': 'video', 'mkv': 'video',
    'avi': 'video', 'video': 'video',
    'mp3': 'audio', 'wav': 'audio', 'ogg': 'audio', 'm4a': 'audio',
    'flac': 'audio', 'aac': 'audio', 'audio': 'audio',
}

#: MIME prefix → type, consulted when the extension says nothing useful.
_MIME_PREFIX_TYPES = (
    ('image/', 'image'),
    ('video/', 'video'),
    ('audio/', 'audio'),
    ('application/pdf', 'pdf'),
    # Last, so a more specific rule above wins: a sniffed `text/*` with no
    # usable extension is text, which is what a README or a `.env` sample is.
    ('text/', 'txt'),
)

#: What an unrecognised file is called (2026-09-20). It used to be `txt`, and
#: that was the bug behind the `.docx`-as-zip-noise class: an unknown *binary*
#: was read as UTF-8 with errors ignored and its noise stored as searchable
#: text. `other` says what is true — we keep the bytes and have no reader for
#: them yet — and `extract_text_from_file` returns nothing rather than rubbish.
#: The file is still downloadable and still readable by `execute_python`
#: through `run_python_on_files`, which is how a rare format gets handled.
DEFAULT_FILE_TYPE = 'other'

#: Types whose bytes *are* text. Everything else is left to a real reader.
TEXT_FILE_TYPES = frozenset({'txt', 'md', 'csv', 'json', 'html'})


def normalize_file_type(filename: str, mime_type: str = '') -> str:
    """Map a filename (and optionally its MIME type) onto a FILE_TYPE_CHOICES value.

    The single place the vocabulary is decided, so every producer of a
    `Document` row agrees with every consumer of `Document.file_type`.
    """
    name = (filename or '').strip()
    ext = name.rsplit('.', 1)[-1].lower() if '.' in name else ''
    if ext in _EXTENSION_TYPES:
        return _EXTENSION_TYPES[ext]

    mime = (mime_type or '').lower()
    for prefix, kind in _MIME_PREFIX_TYPES:
        if mime.startswith(prefix):
            return kind

    return DEFAULT_FILE_TYPE


#: WordprocessingML namespace. A .docx is a zip of XML parts; the text lives in
#: `word/document.xml` under this one namespace.
_W = '{http://schemas.openxmlformats.org/wordprocessingml/2006/main}'


def _docx_paragraph_text(para) -> str:
    """Visible text of one `w:p`, in document order.

    `w:t` runs carry the characters; `w:tab` and `w:br` carry layout that would
    otherwise silently concatenate two words into one.
    """
    out = []
    for node in para.iter():
        if node.tag == _W + 't':
            out.append(node.text or '')
        elif node.tag == _W + 'tab':
            out.append('\t')
        elif node.tag == _W + 'br':
            out.append('\n')
    return ''.join(out)


def _docx_blocks(parent):
    """Yield one line of text per paragraph and per table row, in order.

    Tables are walked as rows rather than flattened paragraph-by-paragraph: a
    four-column row rendered as four separate lines loses which value belonged
    to which heading, which is exactly the association a table exists to carry.
    """
    for child in parent:
        if child.tag == _W + 'p':
            yield _docx_paragraph_text(child)
        elif child.tag == _W + 'tbl':
            for row in child.findall(_W + 'tr'):
                cells = [
                    ' '.join(
                        t for t in (
                            _docx_paragraph_text(p)
                            for p in cell.findall(_W + 'p')
                        ) if t
                    )
                    for cell in row.findall(_W + 'tc')
                ]
                yield '\t'.join(cells)


def extract_docx_text(file_path) -> str:
    """Plain text of a .docx, using the standard library only.

    Deliberately not `python-docx`: the whole job is `w:t` runs in document
    order, the format is a stable OOXML part, and a new wheel in the image is a
    real cost on a 1.9 GB box for about forty lines of walking.

    Returns `''` for anything that is not a readable .docx — above all a legacy
    OLE2 `.doc`, which `normalize_file_type` also files as `docx` and which is
    not a zip at all. An empty string is the honest answer: `vfs.read_file`
    already explains a document with no extracted text, whereas the previous
    behaviour (falling through to `open(..., errors='ignore')`) read the zip
    container as UTF-8 and stored binary noise as the document's content.
    """
    try:
        with zipfile.ZipFile(file_path) as z:
            xml = z.read('word/document.xml')
    except (zipfile.BadZipFile, KeyError, OSError) as e:
        logger.warning('Not a readable .docx (%s): %s', file_path, e)
        return ''

    try:
        root = ElementTree.fromstring(xml)
    except ElementTree.ParseError as e:
        logger.warning('Malformed word/document.xml in %s: %s', file_path, e)
        return ''

    body = root.find(_W + 'body')
    if body is None:
        return ''
    return '\n'.join(_docx_blocks(body))


def extract_xlsx_text(source) -> str:
    """Sheet names, headers and cell values of a workbook, as searchable text.

    Formulas are read as written (`=SUM(B2:B9)`): openpyxl without
    `data_only` returns the formula, and a cached value is only present if
    some spreadsheet application has opened the file. The formula is the
    honest thing to index — it is what the cell contains.
    """
    try:
        import openpyxl

        workbook = openpyxl.load_workbook(source, read_only=True)
    except Exception as e:  # noqa: BLE001 — a corrupt upload is not a crash
        logger.warning('Could not read workbook %s: %s', source, e)
        return ''
    lines = []
    try:
        for sheet in workbook.worksheets:
            lines.append(f'# Sheet: {sheet.title}')
            for row in sheet.iter_rows(max_row=XLSX_EXTRACT_ROWS, values_only=True):
                cells = ['' if v is None else str(v) for v in row]
                if any(cell.strip() for cell in cells):
                    lines.append(' | '.join(cells))
    finally:
        workbook.close()
    return '\n'.join(lines)


def extract_pptx_text(source) -> str:
    """Every slide's text, its tables, and its speaker notes."""
    try:
        from pptx import Presentation

        deck = Presentation(source)
    except Exception as e:  # noqa: BLE001
        logger.warning('Could not read presentation %s: %s', source, e)
        return ''
    lines = []
    for n, slide in enumerate(deck.slides, 1):
        lines.append(f'# Slide {n}')
        for shape in slide.shapes:
            if shape.has_text_frame and shape.text_frame.text.strip():
                lines.append(shape.text_frame.text)
            if getattr(shape, 'has_table', False) and shape.has_table:
                for row in shape.table.rows:
                    lines.append(' | '.join(cell.text for cell in row.cells))
        if slide.has_notes_slide and slide.notes_slide.notes_text_frame.text.strip():
            lines.append(f'Notes: {slide.notes_slide.notes_text_frame.text}')
    return '\n'.join(lines)


class DocumentProcessor:
    #: Formats we can read *today*. Kept as documentation and for the error
    #: message; it is no longer the gate — see `BLOCKED_MIME_TYPES`.
    ALLOWED_MIME_TYPES = [
        'application/pdf',
        'text/plain',
        'text/markdown',
        'application/vnd.openxmlformats-officedocument.wordprocessingml.document',  # .docx
        'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',        # .xlsx
        'application/vnd.openxmlformats-officedocument.presentationml.presentation',  # .pptx
        'application/vnd.ms-excel',
        'application/vnd.ms-powerpoint',
        'application/msword',
        'text/csv',
        'application/json',
        'text/html',
        'image/png',
        'image/jpeg',
        'image/webp',
        'video/mp4',
        'video/quicktime',
        'audio/mpeg',
        'audio/wav',
    ]

    #: **Uploads are allowed by default and refused by exception** (2026-09-20).
    #: An allow-list made the library smaller than the platform: a `.xlsx` could
    #: be *written* by an agent and not *uploaded* by its owner, and every
    #: format we had no parser for was refused rather than kept. Bytes here are
    #: inert — nothing executes an upload, downloads are served
    #: `as_attachment`, and the only code that runs is the model's own in the
    #: sandbox — so keeping an unreadable file costs a row and buys the case
    #: this exists for: `run_python_on_files` opening it later.
    #:
    #: What stays refused is the small set whose only purpose is to be run on
    #: someone's machine. A shared knowledge base is a distribution channel,
    #: and that is the one thing it must not become.
    BLOCKED_MIME_TYPES = frozenset({
        'application/x-dosexec',
        'application/x-msdownload',
        'application/vnd.microsoft.portable-executable',
        'application/x-msi',
        'application/x-executable',
        'application/x-mach-binary',
        'application/x-sharedlib',
        'application/vnd.android.package-archive',
        'application/x-apple-diskimage',
    })
    BLOCKED_EXTENSIONS = frozenset({
        'exe', 'dll', 'msi', 'scr', 'com', 'bat', 'cmd', 'apk', 'dmg', 'jar',
    })

    @classmethod
    def validate_file_upload(cls, file_obj):
        """Validate file size and MIME type; return the sniffed MIME type.

        The MIME type is returned rather than discarded so the caller can feed
        it to `normalize_file_type` — the extension alone is a guess, and a
        file named without one used to be filed as plain text whatever it was.
        """
        if file_obj.size > MAX_DOCUMENT_SIZE:
            raise ValidationError(f"File too large. Maximum size is {MAX_DOCUMENT_SIZE/1024/1024}MB.")

        initial_pos = file_obj.tell()
        try:
            mime_type = magic.from_buffer(file_obj.read(1024), mime=True)
        finally:
            file_obj.seek(initial_pos)

        name = (getattr(file_obj, 'name', '') or '').lower()
        extension = name.rsplit('.', 1)[-1] if '.' in name else ''
        if mime_type in cls.BLOCKED_MIME_TYPES or extension in cls.BLOCKED_EXTENSIONS:
            raise ValidationError(
                "Executable files cannot be uploaded. Everything else is "
                "accepted; a format we cannot read yet is stored as-is and can "
                "be opened with Python."
            )

        return mime_type

    @staticmethod
    def sanitize_document_content(content):
        """Sanitize text content using bleach."""
        if not content:
            return ""
        return bleach.clean(content, tags=[], strip=True)

    @classmethod
    def extract_text_from_file(cls, file_path, file_type):
        """Extract plain text from various file formats."""
        text = ""
        file_type = file_type.lower()

        try:
            if file_type in ('image', 'video', 'audio', 'other'):
                # Nothing to read: `other` is a format we keep but cannot parse
                # yet, and reading it as UTF-8 is what stored zip noise as
                # searchable text before 2026-09-04.
                return ""

            if file_type == 'pdf':
                reader = PdfReader(file_path)
                for page in reader.pages:
                    text += page.extract_text() + "\n"

            elif file_type == 'json':
                with open(file_path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    text = json.dumps(data, indent=2)

            elif file_type == 'docx':
                # Accepted at upload since the first migration and handled
                # nowhere: a .docx fell to the `else` below, which read the zip
                # container as UTF-8 with errors ignored and stored the noise
                # as `content_text` — then chunked and embedded it.
                text = extract_docx_text(file_path)

            elif file_type == 'xlsx':
                text = extract_xlsx_text(file_path)

            elif file_type == 'pptx':
                text = extract_pptx_text(file_path)

            elif file_type == 'csv':
                with open(file_path, 'r', encoding='utf-8') as f:
                    reader = csv.reader(f)
                    for row in reader:
                        text += " ".join(row) + "\n"

            elif file_type in TEXT_FILE_TYPES:
                with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
                    text = f.read()

            else:
                # A type we have no reader for. Keep the file, store no text —
                # a searchable index of mojibake is worse than an empty one.
                return ""

        except Exception as e:
            logger.error(f"Error extracting text from {file_path}: {e}")
            return ""

        return cls.sanitize_document_content(text)


# Module-level aliases for backward compatibility
ALLOWED_MIME_TYPES = DocumentProcessor.ALLOWED_MIME_TYPES
validate_file_upload = DocumentProcessor.validate_file_upload
__all__ = [
    'ALLOWED_MIME_TYPES', 'DEFAULT_FILE_TYPE', 'DocumentProcessor',
    'TEXT_FILE_TYPES', 'extract_docx_text', 'extract_pptx_text',
    'extract_xlsx_text', 'extract_text_from_file', 'normalize_file_type',
    'sanitize_document_content', 'user_document_path', 'validate_file_upload',
]
sanitize_document_content = DocumentProcessor.sanitize_document_content
extract_text_from_file = DocumentProcessor.extract_text_from_file
