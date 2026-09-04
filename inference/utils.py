import csv
import json
import logging
import os
import uuid

import bleach
import magic
from django.core.exceptions import ValidationError
from pypdf import PdfReader

from workflow_backend.thresholds import MAX_DOCUMENT_SIZE

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
    'csv': 'csv', 'tsv': 'csv',
    'json': 'json',
    'html': 'html', 'htm': 'html',
    'png': 'image', 'jpg': 'image', 'jpeg': 'image', 'webp': 'image',
    'gif': 'image', 'bmp': 'image', 'tiff': 'image', 'tif': 'image',
    'image': 'image',
    'mp4': 'video', 'mov': 'video', 'webm': 'video', 'mkv': 'video',
    'avi': 'video', 'video': 'video',
}

#: MIME prefix → type, consulted when the extension says nothing useful.
_MIME_PREFIX_TYPES = (
    ('image/', 'image'),
    ('video/', 'video'),
    ('application/pdf', 'pdf'),
)

#: What an unrecognised file is called. Text extraction treats it as text,
#: which is the old behaviour for anything unknown.
DEFAULT_FILE_TYPE = 'txt'


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


class DocumentProcessor:
    ALLOWED_MIME_TYPES = [
        'application/pdf',
        'text/plain',
        'text/markdown',
        'application/vnd.openxmlformats-officedocument.wordprocessingml.document',  # .docx
        'text/csv',
        'application/json',
        'text/html',
        'image/png',
        'image/jpeg',
        'image/webp',
        'video/mp4',
        'video/quicktime',
    ]

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

        if mime_type not in cls.ALLOWED_MIME_TYPES:
            raise ValidationError(f"Unsupported file type: {mime_type}. Allowed types: PDF, Text, Markdown, Docx, CSV, JSON, HTML.")

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
            if file_type in ('image', 'video'):
                return ""

            if file_type == 'pdf':
                reader = PdfReader(file_path)
                for page in reader.pages:
                    text += page.extract_text() + "\n"

            elif file_type == 'json':
                with open(file_path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    text = json.dumps(data, indent=2)

            elif file_type == 'csv':
                with open(file_path, 'r', encoding='utf-8') as f:
                    reader = csv.reader(f)
                    for row in reader:
                        text += " ".join(row) + "\n"

            else:
                with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
                    text = f.read()

        except Exception as e:
            logger.error(f"Error extracting text from {file_path}: {e}")
            return ""

        return cls.sanitize_document_content(text)


# Module-level aliases for backward compatibility
ALLOWED_MIME_TYPES = DocumentProcessor.ALLOWED_MIME_TYPES
validate_file_upload = DocumentProcessor.validate_file_upload
__all__ = [
    'ALLOWED_MIME_TYPES', 'DEFAULT_FILE_TYPE', 'DocumentProcessor',
    'extract_text_from_file', 'normalize_file_type',
    'sanitize_document_content', 'user_document_path', 'validate_file_upload',
]
sanitize_document_content = DocumentProcessor.sanitize_document_content
extract_text_from_file = DocumentProcessor.extract_text_from_file
