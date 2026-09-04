from django.contrib import admin

from .models import (
    Document, DocumentChunk, ExtractedRow, ExtractionSchema, Folder,
    IndexedTerm, KnowledgeBase,
)


class DocumentChunkInline(admin.TabularInline):
    model = DocumentChunk
    extra = 0
    readonly_fields = ['chunk_index', 'token_count', 'start_char', 'end_char', 
                       'page_number', 'embedding_model']
    fields = ['chunk_index', 'token_count', 'page_number', 'content']
    ordering = ['chunk_index']
    max_num = 20
    can_delete = False
    
    def has_add_permission(self, request, obj=None):
        return False


@admin.register(Document)
class DocumentAdmin(admin.ModelAdmin):
    list_display = ['name', 'user', 'file_type', 'file_size', 'status',
                    'chunk_count', 'indexed_at', 'created_at']
    list_filter = ['file_type', 'status', 'created_at']
    search_fields = ['name', 'document_id', 'user__username', 'folder__name']
    readonly_fields = ['document_id', 'file_size', 'chunk_count', 
                       'indexed_at', 'created_at', 'updated_at']
    raw_id_fields = ['folder']

    def get_queryset(self, request):
        # Admin is the one place trashed rows should stay visible — the
        # default manager hides them from everything else on purpose.
        return Document.all_objects.get_queryset()
    ordering = ['-created_at']
    inlines = [DocumentChunkInline]
    
    fieldsets = (
        ('Document Info', {
            'fields': ('document_id', 'user', 'name', 'file', 'file_type', 'file_size')
        }),
        ('Processing', {
            'fields': ('status', 'error_message')
        }),
        ('Content', {
            'fields': ('content_text', 'metadata'),
            'classes': ('collapse',)
        }),
        ('Indexing', {
            'fields': ('chunk_count', 'indexed_at')
        }),
        ('Organization', {
            'fields': ('folder', 'tags')
        }),
        ('Recycle bin', {
            'fields': ('deleted_at', 'trashed_directly'),
            'classes': ('collapse',),
            'description': 'Trash is a state, not a place. A row with '
                           'deleted_at set is invisible everywhere except here.'
        }),
        ('Timestamps', {
            'fields': ('created_at', 'updated_at'),
            'classes': ('collapse',)
        }),
    )


@admin.register(DocumentChunk)
class DocumentChunkAdmin(admin.ModelAdmin):
    list_display = ['document', 'chunk_index', 'token_count', 'page_number',
                    'embedding_model', 'content_preview']
    list_filter = ['embedding_model', 'created_at']
    search_fields = ['document__name', 'content']
    readonly_fields = ['created_at']
    ordering = ['document', 'chunk_index']
    
    fieldsets = (
        ('Chunk Info', {
            'fields': ('document', 'chunk_index', 'token_count')
        }),
        ('Content', {
            'fields': ('content',)
        }),
        ('Position', {
            'fields': ('start_char', 'end_char', 'page_number')
        }),
        ('Embedding', {
            'fields': ('embedding_model', 'embedding'),
            'classes': ('collapse',),
            'description': 'Vector embedding data'
        }),
        ('Metadata', {
            'fields': ('metadata', 'created_at'),
            'classes': ('collapse',)
        }),
    )
    
    def content_preview(self, obj):
        if len(obj.content) > 50:
            return obj.content[:50] + "..."
        return obj.content
    content_preview.short_description = 'Content'


@admin.register(KnowledgeBase)
class KnowledgeBaseAdmin(admin.ModelAdmin):
    list_display = ['name', 'user', 'backend', 'doc_count', 'vector_count',
                    'is_default', 'updated_at']
    list_filter = ['backend', 'is_default', 'created_at']
    search_fields = ['name', 'user__username', 'description']
    readonly_fields = ['doc_count', 'vector_count', 'index_size_bytes',
                       'created_at', 'updated_at']
    ordering = ['-created_at']


@admin.register(IndexedTerm)
class IndexedTermAdmin(admin.ModelAdmin):
    list_display = ['term', 'term_frequency', 'kb', 'document', 'chunk']
    # No list_filter on `term`: the inverted index is the largest table in the
    # app, and a filter sidebar would enumerate every distinct token in it.
    list_filter = ['kb']
    search_fields = ['term']
    raw_id_fields = ['kb', 'document', 'chunk']


@admin.register(ExtractionSchema)
class ExtractionSchemaAdmin(admin.ModelAdmin):
    list_display = ['name', 'user', 'field_count', 'source_kind',
                    'confidence_threshold', 'updated_at']
    list_filter = ['source_kind', 'created_at']
    search_fields = ['name', 'user__username', 'description']
    readonly_fields = ['created_at', 'updated_at']
    ordering = ['-updated_at']


@admin.register(ExtractedRow)
class ExtractedRowAdmin(admin.ModelAdmin):
    list_display = ['document_name', 'schema', 'confidence', 'status',
                    'reviewed_by', 'reviewed_at', 'created_at']
    list_filter = ['status', 'created_at']
    search_fields = ['document_name', 'schema__name']
    readonly_fields = ['created_at']
    raw_id_fields = ['schema', 'document', 'reviewed_by']
    ordering = ['-created_at']


@admin.register(Folder)
class FolderAdmin(admin.ModelAdmin):
    list_display = ['name', 'user', 'path', 'depth', 'deleted_at']
    list_filter = ['depth', 'created_at']
    search_fields = ['name', 'user__username', 'path']
    readonly_fields = ['path', 'depth', 'created_at', 'updated_at']
    raw_id_fields = ['user', 'parent']
    ordering = ['user', 'path']

    def get_queryset(self, request):
        # See DocumentAdmin — admin is where trash stays visible.
        return Folder.all_objects.get_queryset()
