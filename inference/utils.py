import magic
import bleach
from django.core.exceptions import ValidationError

# 50MB limit
MAX_DOCUMENT_SIZE = 50 * 1024 * 1024

ALLOWED_MIME_TYPES = [
    'application/pdf',
    'text/plain',
    'text/markdown',
    'application/vnd.openxmlformats-officedocument.wordprocessingml.document', # .docx
    'text/csv',
    'application/json',
    'text/html',
]

def validate_file_upload(file_obj):
    """
    Validate file size and MIME type.
    """
    # Check size
    if file_obj.size > MAX_DOCUMENT_SIZE:
        raise ValidationError(f"File too large. Maximum size is {MAX_DOCUMENT_SIZE/1024/1024}MB.")
    
    # Check MIME type
    # Read first 1024 bytes for magic number check
    initial_pos = file_obj.tell()
    try:
        mime_type = magic.from_buffer(file_obj.read(1024), mime=True)
    finally:
        file_obj.seek(initial_pos)
        
    if mime_type not in ALLOWED_MIME_TYPES:
        raise ValidationError(f"Unsupported file type: {mime_type}. Allowed types: PDF, Text, Markdown, Docx, CSV, JSON, HTML.")
    
    return True

def sanitize_document_content(content):
    """
    Sanitize text content using bleach.
    """
    if not content:
        return ""
    
    # Strip ALL tags, we only want plain text for RAG
    # We can refine this to allow some basic formatting if needed later
    return bleach.clean(content, tags=[], strip=True)

import json
import csv
from io import StringIO
from pypdf import PdfReader

def extract_text_from_file(file_path, file_type):
    """
    Extract plain text from various file formats.
    """
    text = ""
    file_type = file_type.lower()
    
    try:
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
            # Default to text/markdown/html
            with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
                text = f.read()
                
    except Exception as e:
        print(f"Error extracting text from {file_path}: {e}")
        return ""
        
    return sanitize_document_content(text)
