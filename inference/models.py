from django.db import models
from django.conf import settings
from django.core.validators import MinValueValidator
import uuid


class Document(models.Model):
    """
    Uploaded documents for RAG (Retrieval-Augmented Generation).
    Stores files and metadata for knowledge base queries.
    """
    STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('processing', 'Processing'),
        ('indexed', 'Indexed'),
        ('failed', 'Failed'),
    ]
    
    FILE_TYPE_CHOICES = [
        ('pdf', 'PDF'),
        ('txt', 'Text'),
        ('md', 'Markdown'),
        ('docx', 'Word Document'),
        ('csv', 'CSV'),
        ('json', 'JSON'),
        ('html', 'HTML'),
    ]
    
    document_id = models.UUIDField(
        default=uuid.uuid4,
        editable=False,
        unique=True
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='documents'
    )
    
    # File Info
    name = models.CharField(
        max_length=255,
        help_text='Original filename'
    )
    file = models.FileField(
        upload_to=''
    )
    file_type = models.CharField(
        max_length=10,
        choices=FILE_TYPE_CHOICES
    )
    file_size = models.IntegerField(
        validators=[MinValueValidator(0)],
        help_text='File size in bytes'
    )
    
    # Processing
    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default='pending'
    )
    error_message = models.TextField(
        blank=True,
        help_text='Error message if processing failed'
    )
    
    # Content
    content_text = models.TextField(
        blank=True,
        help_text='Extracted text content'
    )
    metadata = models.JSONField(
        default=dict,
        blank=True,
        help_text='Document metadata (title, author, etc.)'
    )
    
    # Indexing Stats
    chunk_count = models.IntegerField(
        default=0,
        help_text='Number of chunks created'
    )
    indexed_at = models.DateTimeField(
        blank=True,
        null=True
    )
    
    # Organization
    tags = models.JSONField(
        default=list,
        blank=True
    )
    folder = models.CharField(
        max_length=255,
        blank=True,
        help_text='Virtual folder path for organization'
    )
    
    # Platform Sharing - User can opt-in to share document with platform KB
    is_shared = models.BooleanField(
        default=False,
        help_text='If True, document is shared with platform knowledge base'
    )
    shared_at = models.DateTimeField(
        blank=True,
        null=True,
        help_text='When the document was shared with platform'
    )
    
    # Timestamps
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'Document'
        verbose_name_plural = 'Documents'
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['document_id']),
            models.Index(fields=['user', 'status']),
            models.Index(fields=['user', '-created_at']),
            models.Index(fields=['file_type', 'status']),
            models.Index(fields=['is_shared', 'status']),  # For platform KB queries
        ]

    def __str__(self):
        return self.name

    @property
    def is_indexed(self):
        """Check if document has been indexed"""
        return self.status == 'indexed'


class DocumentChunk(models.Model):
    """
    Chunked text from documents for vector search.
    Each chunk is embedded and stored for RAG queries.
    """
    document = models.ForeignKey(
        Document,
        on_delete=models.CASCADE,
        related_name='chunks'
    )
    
    # Chunk Info
    chunk_index = models.IntegerField(
        help_text='Position of this chunk in the document'
    )
    content = models.TextField(
        help_text='Text content of this chunk'
    )
    token_count = models.IntegerField(
        default=0,
        help_text='Number of tokens in this chunk'
    )
    
    # Position in Document
    start_char = models.IntegerField(
        default=0,
        help_text='Starting character position'
    )
    end_char = models.IntegerField(
        default=0,
        help_text='Ending character position'
    )
    page_number = models.IntegerField(
        blank=True,
        null=True,
        help_text='Page number if applicable'
    )
    
    # Embedding
    embedding = models.BinaryField(
        blank=True,
        null=True,
        help_text='Vector embedding (stored as binary)'
    )
    embedding_model = models.CharField(
        max_length=100,
        blank=True,
        help_text='Model used for embedding'
    )
    
    # Metadata
    metadata = models.JSONField(
        default=dict,
        blank=True,
        help_text='Chunk-level metadata (headings, etc.)'
    )
    
    # Timestamps
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = 'Document Chunk'
        verbose_name_plural = 'Document Chunks'
        ordering = ['document', 'chunk_index']
        unique_together = ['document', 'chunk_index']
        indexes = [
            models.Index(fields=['document', 'chunk_index']),
        ]

    def __str__(self):
        preview = self.content[:50] + "..." if len(self.content) > 50 else self.content
        return f"Chunk {self.chunk_index}: {preview}"
