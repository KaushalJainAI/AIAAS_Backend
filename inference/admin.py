from django.contrib import admin
from .models import Document, DocumentChunk


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
    search_fields = ['name', 'document_id', 'user__username', 'folder']
    readonly_fields = ['document_id', 'file_size', 'chunk_count', 
                       'indexed_at', 'created_at', 'updated_at']
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
