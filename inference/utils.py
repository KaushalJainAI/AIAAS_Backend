import json
import csv
import logging
import magic
import bleach
from io import StringIO
from pypdf import PdfReader
from django.core.exceptions import ValidationError
from workflow_backend.thresholds import MAX_DOCUMENT_SIZE

logger = logging.getLogger(__name__)


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
        """Validate file size and MIME type."""
        if file_obj.size > MAX_DOCUMENT_SIZE:
            raise ValidationError(f"File too large. Maximum size is {MAX_DOCUMENT_SIZE/1024/1024}MB.")

        initial_pos = file_obj.tell()
        try:
            mime_type = magic.from_buffer(file_obj.read(1024), mime=True)
        finally:
            file_obj.seek(initial_pos)

        if mime_type not in cls.ALLOWED_MIME_TYPES:
            raise ValidationError(f"Unsupported file type: {mime_type}. Allowed types: PDF, Text, Markdown, Docx, CSV, JSON, HTML.")

        return True

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
sanitize_document_content = DocumentProcessor.sanitize_document_content
extract_text_from_file = DocumentProcessor.extract_text_from_file
