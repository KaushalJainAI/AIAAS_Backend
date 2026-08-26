from django.db import models
from django.conf import settings
from django.core.validators import MinValueValidator
import uuid

from .utils import user_document_path


class LiveManager(models.Manager):
    """Rows a user can still see — everything not in the recycle bin.

    This is the **default** manager on `Folder` and `Document`, and that is the
    whole design of trash. A recycle bin implemented as a reserved folder you
    reparent into would make every listing in the codebase responsible for
    remembering to exclude one magic id, and forgetting would be silent. A
    default manager is the one guard that cannot be forgotten: `kb.documents`,
    `chat/tools/knowledge.py`, `inference/signals.py` and every query written
    next year inherit it without knowing it exists.

    Django's `_base_manager` stays unfiltered (we deliberately do not set
    `Meta.base_manager_name`), so FK traversal, cascade collection and
    migrations still reach trashed rows — which is exactly what restore and the
    purge sweep need. Use `all_objects` to ask for them by name.
    """

    def get_queryset(self):
        return super().get_queryset().filter(deleted_at__isnull=True)


class Folder(models.Model):
    """One node of a user's private document tree.

    A tree contains only its owner's rows. Documents shared platform-wide
    (`Document.sharing_mode`) stay a flat public library and are never a place
    in anyone's tree — so "can this user reach this node" has exactly one
    answer, checked in exactly one place (`inference/filesystem.py`).

    Orthogonal to `KnowledgeBase`: a folder organises, a KB indexes. A document
    has both, independently, and moving it between folders touches no vectors.
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='folders',
    )
    parent = models.ForeignKey(
        'self',
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name='children',
        help_text=(
            "NULL is the user's root. There is deliberately no root row: NULL "
            'is unforgeable — it can never be another user\'s folder — so the '
            'root is one fewer id on the attack surface, and a root row would '
            'be a second spelling of "in root" that the chat upload paths '
            'would never write.'
        ),
    )
    name = models.CharField(max_length=255)

    path = models.CharField(
        max_length=1000,
        blank=True,
        db_index=True,
        help_text=(
            'Materialised ancestry as slash-delimited *ids*, self-inclusive: '
            '"/12/45/78/". Ids, not names — a name path would make rename '
            'O(descendants) and would put a user-controlled path string back '
            'in the database. Written by save(); never accepted from a client.'
        ),
    )
    depth = models.PositiveSmallIntegerField(default=0)

    deleted_at = models.DateTimeField(null=True, blank=True, db_index=True)
    trashed_directly = models.BooleanField(
        default=False,
        help_text=(
            'True only on the node the user actually deleted. Descendants '
            'carry deleted_at but not this, so the trash view lists one entry '
            'per delete instead of one per row in the subtree.'
        ),
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = LiveManager()      # declared first: this is _default_manager
    all_objects = models.Manager()

    class Meta:
        verbose_name = 'Folder'
        verbose_name_plural = 'Folders'
        ordering = ['name']
        constraints = [
            models.UniqueConstraint(
                fields=['user', 'parent', 'name'],
                condition=models.Q(parent__isnull=False, deleted_at__isnull=True),
                name='unique_folder_name_per_parent',
            ),
            # SQL treats NULLs as distinct, so the constraint above does not
            # reach top-level folders at all. Two constraints, one rule.
            models.UniqueConstraint(
                fields=['user', 'name'],
                condition=models.Q(parent__isnull=True, deleted_at__isnull=True),
                name='unique_root_folder_name_per_user',
            ),
        ]
        indexes = [
            models.Index(fields=['user', 'parent']),
            models.Index(fields=['user', 'path']),
            models.Index(fields=['user', 'deleted_at']),
        ]

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        if self.parent_id and self.parent.user_id != self.user_id:
            # The last line of defence behind inference/filesystem.py. If a
            # future code path ever skips the choke point, this makes it a 500
            # rather than a breach.
            raise ValueError('A folder may not be parented outside its owner.')

        super().save(*args, **kwargs)

        prefix = self.parent.path if self.parent_id else '/'
        path = f'{prefix}{self.pk}/'
        depth = (self.parent.depth + 1) if self.parent_id else 0
        if (self.path, self.depth) != (path, depth):
            # Second write on create only: the id does not exist until the
            # first one has run.
            self.path, self.depth = path, depth
            super().save(update_fields=['path', 'depth'])

    @property
    def ancestor_ids(self) -> list:
        """Ids from the root down to (but excluding) this folder."""
        return [int(part) for part in self.path.strip('/').split('/') if part][:-1]


class KnowledgeBase(models.Model):
    """
    A named, persistent knowledge base owned by a user.

    The `backend` decides what ingestion and retrieval mean for this KB —
    the container is the stable thing users and agents talk to; the
    machinery behind it is one of the registered backends in
    `inference/backends/`:

      vector   chunk + embed, HNSW semantic search (the original behaviour)
      fulltext chunk only, inverted-index keyword search (exact/prefix/phrase)
      raw      store extracted text; retrieval is the agent reading whole
               documents (list_documents + read_document), no search index
      hybrid   vector + fulltext together, results merged by rank fusion
    """

    BACKEND_VECTOR = 'vector'
    BACKEND_FULLTEXT = 'fulltext'
    BACKEND_RAW = 'raw'
    BACKEND_HYBRID = 'hybrid'

    BACKEND_CHOICES = [
        (BACKEND_VECTOR, 'Vector (semantic)'),
        (BACKEND_FULLTEXT, 'Keyword (exact / prefix match)'),
        (BACKEND_RAW, 'Raw (agent reads whole documents)'),
        (BACKEND_HYBRID, 'Hybrid (semantic + keyword)'),
    ]

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='knowledge_bases'
    )
    name = models.CharField(max_length=255, default='Default')
    description = models.TextField(blank=True)
    backend = models.CharField(
        max_length=20,
        choices=BACKEND_CHOICES,
        default=BACKEND_VECTOR,
        help_text='Retrieval machinery this KB uses. Fixed once documents '
                  'are ingested — switching would orphan whatever the old '
                  'backend stored.'
    )
    embedding_model = models.CharField(
        max_length=100,
        default='nvidia/nv-embedqa-e5-v5',
        help_text='Embedding model used for this KB (vector/hybrid backends only)'
    )
    vector_dim = models.IntegerField(default=1024)
    doc_count = models.IntegerField(default=0)
    vector_count = models.IntegerField(default=0)
    index_size_bytes = models.BigIntegerField(default=0)
    s3_index_key = models.CharField(
        max_length=500,
        blank=True,
        help_text='S3 key for the FAISS index bundle (not publicly downloadable)'
    )
    is_default = models.BooleanField(
        default=False,
        help_text='User\'s default KB — auto-created on first document upload'
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'Knowledge Base'
        verbose_name_plural = 'Knowledge Bases'
        ordering = ['-created_at']
        constraints = [
            models.UniqueConstraint(
                fields=['user', 'name'],
                name='unique_kb_name_per_user'
            )
        ]
        indexes = [
            models.Index(fields=['user', 'is_default']),
        ]

    def __str__(self):
        return f"{self.name} ({self.user})"

    # Index file locations are owned by `inference.engine.HNSWKnowledgeBase`
    # (`_local_index_path` / `_local_docs_path`). The duplicates that used to
    # sit here had no callers and were a second place for the naming scheme to
    # drift out of step with the code that actually reads the files.

    @property
    def uses_embeddings(self):
        """Whether this KB's ingestion embeds anything."""
        return self.backend in (self.BACKEND_VECTOR, self.BACKEND_HYBRID)

    @property
    def uses_fulltext(self):
        """Whether this KB maintains the keyword inverted index."""
        return self.backend in (self.BACKEND_FULLTEXT, self.BACKEND_HYBRID)


class Document(models.Model):
    """
    Uploaded documents for RAG (Retrieval-Augmented Generation).
    Stores files and metadata for knowledge base queries.
    """
    STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('processing', 'Processing'),
        ('indexed', 'Indexed'),
        # Raw-backend terminal state: text extracted, nothing indexed. The
        # document is retrievable by browsing and reading, not by search.
        ('stored', 'Stored'),
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
        ('image', 'Image'),
        ('video', 'Video'),
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
    knowledge_base = models.ForeignKey(
        KnowledgeBase,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='documents',
        help_text='KB this document is indexed into'
    )
    
    # File Info
    name = models.CharField(
        max_length=255,
        help_text='Original filename'
    )
    file = models.FileField(
        upload_to=user_document_path
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
    folder = models.ForeignKey(
        Folder,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='documents',
        help_text=(
            "NULL is the user's root. SET_NULL, emphatically not CASCADE: a "
            'cascade would take Documents out through the ORM collector, which '
            'fires post_delete but never runs tasks.remove_document_from_kb — '
            'leaving FAISS vectors and IndexedTerm postings for rows that no '
            'longer exist, which RAG then answers with a dangling id. Losing a '
            'folder must therefore never lose the files in it; they surface at '
            'the root instead, still indexed and still findable. '
            'PROTECT was tried first, to make the database enforce the purge '
            "sweep's documents-before-folders ordering. It cannot be used: "
            "Django evaluates on_delete during the collector's *collection* "
            'pass, with no exemption for protected rows that are themselves '
            'being collected, so deleting a user — where Folder and Document '
            'both cascade off `user` — raised ProtectedError and account '
            'deletion was impossible. The ordering is enforced by '
            '`recycle.run_recycle_sweep` and pinned by its tests instead.'
        ),
    )

    # Recycle bin. Trash is a state, not a place — see LiveManager.
    deleted_at = models.DateTimeField(null=True, blank=True, db_index=True)
    trashed_directly = models.BooleanField(
        default=False,
        help_text='True only on the row the user actually deleted.',
    )
    
    # Platform Sharing
    SHARING_MODE_CHOICES = [
        ('private', 'Private'),
        ('shared_read', 'Shared (Read-Only)'),
        ('shared_write', 'Shared (Read/Write)'),
    ]
    sharing_mode = models.CharField(
        max_length=20,
        choices=SHARING_MODE_CHOICES,
        default='private',
        help_text='Privacy setting for this document'
    )
    is_shared = models.BooleanField(
        default=False,
        help_text=(
            'Superseded by sharing_mode; kept as a mirror of '
            "(sharing_mode != 'private') for older clients that still read it. "
            'Maintained by Document.save(), never set independently — read '
            'sharing_mode in new code.'
        )
    )
    shared_at = models.DateTimeField(
        blank=True,
        null=True,
        help_text='When the document was shared with platform'
    )
    
    # Timestamps
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = LiveManager()      # declared first: this is _default_manager
    all_objects = models.Manager()

    class Meta:
        verbose_name = 'Document'
        verbose_name_plural = 'Documents'
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['document_id']),
            models.Index(fields=['user', 'status']),
            models.Index(fields=['user', '-created_at']),
            models.Index(fields=['user', '-created_at', '-id']),
            models.Index(fields=['file_type', 'status']),
            models.Index(fields=['sharing_mode', 'status']),  # For platform KB queries
            models.Index(fields=['sharing_mode', '-created_at', '-id']),
            models.Index(fields=['user', 'folder', '-created_at']),
            models.Index(fields=['user', 'deleted_at']),
        ]

    def save(self, *args, **kwargs):
        from .utils import sanitize_document_content
        if self.folder_id and self.folder.user_id != self.user_id:
            # Mirrors Folder.save() — the last line of defence behind
            # inference/filesystem.py.
            raise ValueError('A document may not be filed outside its owner.')
        if self.content_text:
            self.content_text = sanitize_document_content(self.content_text)
        # Keep the legacy mirror true by construction rather than by every
        # writer remembering to set both. `update_fields` writes have to be
        # told about it, or the column silently stops tracking.
        derived = self.sharing_mode != 'private'
        if self.is_shared != derived:
            self.is_shared = derived
            update_fields = kwargs.get('update_fields')
            if update_fields is not None and 'sharing_mode' in update_fields:
                kwargs['update_fields'] = list(update_fields) + ['is_shared']
        super().save(*args, **kwargs)

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


class IndexedTerm(models.Model):
    """
    One posting of the keyword inverted index behind the fulltext backend:
    a term that occurs (term_frequency times) in one chunk of one document.

    Deliberately our own table rather than Postgres FTS / SQLite FTS5: it
    behaves identically on both engines this project deploys, and matching
    semantics stay ours — case-insensitive exact terms plus bounded prefix
    expansion, which is what gives the grep-like feel embeddings cannot.

    Rows cascade away with their chunk, so re-indexing or deleting a
    document can never leave stale postings behind.
    """
    kb = models.ForeignKey(
        KnowledgeBase,
        on_delete=models.CASCADE,
        related_name='index_terms',
    )
    term = models.CharField(max_length=100)
    document = models.ForeignKey(
        Document,
        on_delete=models.CASCADE,
        related_name='term_hits',
    )
    chunk = models.ForeignKey(
        DocumentChunk,
        on_delete=models.CASCADE,
        related_name='term_hits',
    )
    term_frequency = models.IntegerField(default=1)

    class Meta:
        verbose_name = 'Indexed Term'
        verbose_name_plural = 'Indexed Terms'
        constraints = [
            models.UniqueConstraint(
                fields=['chunk', 'term'],
                name='unique_term_per_chunk',
            )
        ]
        indexes = [
            models.Index(fields=['kb', 'term']),
            models.Index(fields=['document']),
        ]

    def __str__(self):
        return f'{self.term} ×{self.term_frequency} (chunk {self.chunk_id})'


#: Model used to extract schema fields from a document when the schema does not
#: pin one of its own. Vision-capable: an image document is read by pixels.
DEFAULT_EXTRACTION_MODEL = 'nvidia/nemotron-nano-12b-v2-vl'


class ExtractionSchema(models.Model):
    """
    A named extraction contract: the fields to pull out of a document, and the
    bar that decides whether a row is trusted or held for review.

    The rule that makes the output usable for accounting is that low-confidence
    fields are flagged, not quietly guessed. So confidence is stored per row and
    the threshold lives on the schema: what counts as "sure enough" for a
    delivery challan is not what counts for a GST certificate, and hardcoding
    one number would force the stricter case to accept the looser one's
    mistakes.
    """
    SOURCE_CHOICES = [
        ('upload', 'Manual upload'),
        ('gmail', 'Gmail'),
        ('gdrive', 'Google Drive'),
    ]

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='extraction_schemas'
    )
    name = models.CharField(max_length=200)
    description = models.TextField(blank=True)

    # [{"name": "gstin", "label": "GSTIN", "type": "string", "required": true}, ...]
    fields = models.JSONField(default=list, help_text='The columns to fill')

    source_kind = models.CharField(max_length=20, choices=SOURCE_CHOICES, default='upload')
    source_ref = models.CharField(
        max_length=300, blank=True, help_text='Label, folder or query the documents come from'
    )

    confidence_threshold = models.FloatField(
        default=0.8, help_text='Below this, a row is held for review rather than accepted'
    )

    llm_model = models.CharField(
        max_length=200, blank=True,
        help_text='Model used to extract rows. Blank resolves to the default vision model.',
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-updated_at']
        unique_together = ['user', 'name']
        indexes = [models.Index(fields=['user', '-updated_at'])]

    def __str__(self):
        return self.name

    @property
    def field_count(self):
        return len(self.fields) if isinstance(self.fields, list) else 0

    @property
    def effective_model(self):
        return self.llm_model.strip() or DEFAULT_EXTRACTION_MODEL


class ExtractedRow(models.Model):
    """
    One document's extraction against a schema, plus the audit of who cleared it.

    `confidence` is the *lowest* field confidence in the row: a row where eight
    of nine fields are perfect and GSTIN is 0.41 must be held, because one wrong
    tax number invalidates the row. `apply_threshold()` derives `status` from
    confidence on every write so the flag can never disagree with the number it
    is computed from — and a human `reviewed`/`rejected` decision is terminal.
    """
    STATUS_CHOICES = [
        ('accepted', 'Accepted'),
        ('needs_review', 'Needs review'),
        ('reviewed', 'Reviewed'),
        ('rejected', 'Rejected'),
    ]

    schema = models.ForeignKey(ExtractionSchema, on_delete=models.CASCADE, related_name='rows')
    document_name = models.CharField(max_length=300)
    document = models.ForeignKey(
        Document,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='extracted_rows',
    )

    data = models.JSONField(default=dict, help_text='Field name -> extracted value')
    field_confidence = models.JSONField(
        default=dict, blank=True, help_text='Field name -> confidence, so review can point at the cell'
    )
    confidence = models.FloatField(default=0.0, help_text='Lowest field confidence in the row')

    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='accepted', db_index=True)
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='reviewed_extractions',
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['schema', 'status']),
            models.Index(fields=['schema', '-created_at']),
        ]

    def __str__(self):
        return f'{self.document_name} ({self.schema.name})'

    def apply_threshold(self):
        """Set status from confidence. Called on write so the flag can never
        disagree with the number it is derived from."""
        if self.status in ('reviewed', 'rejected'):
            return
        self.status = (
            'needs_review' if self.confidence < self.schema.confidence_threshold else 'accepted'
        )
