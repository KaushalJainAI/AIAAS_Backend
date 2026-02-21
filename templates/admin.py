from django.contrib import admin
from .models import WorkflowTemplate, WorkflowRating, WorkflowBookmark, TemplateComment

@admin.register(WorkflowTemplate)
class WorkflowTemplateAdmin(admin.ModelAdmin):
    list_display = ('name', 'category', 'status', 'author_name', 'usage_count', 'average_rating', 'is_featured', 'created_at')
    list_filter = ('category', 'status', 'is_featured', 'created_at')
    search_fields = ('name', 'description', 'author_name', 'tags')
    readonly_fields = ('usage_count', 'average_rating', 'rating_count', 'fork_count', 'embedding', 'created_at', 'updated_at')
    fieldsets = (
        ('Basic Information', {
            'fields': ('name', 'description', 'category', 'tags', 'is_featured')
        }),
        ('Author Details', {
            'fields': ('author', 'author_name')
        }),
        ('Definition', {
            'fields': ('nodes', 'edges', 'workflow_settings')
        }),
        ('Lifecycle & Status', {
            'fields': ('status', 'parent_template', 'source_workflow_id')
        }),
        ('Metrics & Quality', {
            'fields': ('usage_count', 'average_rating', 'rating_count', 'success_rate', 'average_duration_ms', 'fork_count')
        }),
        ('AI & Search', {
            'fields': ('embedding', 'modifiable_fields', 'is_cloneable')
        }),
        ('Timestamps', {
            'fields': ('created_at', 'updated_at'),
            'classes': ('collapse',)
        }),
    )

@admin.register(WorkflowRating)
class WorkflowRatingAdmin(admin.ModelAdmin):
    list_display = ('template', 'user', 'stars', 'created_at')
    list_filter = ('stars', 'created_at')
    search_fields = ('template__name', 'user__username', 'review')

@admin.register(WorkflowBookmark)
class WorkflowBookmarkAdmin(admin.ModelAdmin):
    list_display = ('template', 'user', 'created_at')
    search_fields = ('template__name', 'user__username')

@admin.register(TemplateComment)
class TemplateCommentAdmin(admin.ModelAdmin):
    list_display = ('template', 'user', 'text_preview', 'parent', 'created_at')
    list_filter = ('created_at',)
    search_fields = ('template__name', 'user__username', 'text')

    def text_preview(self, obj):
        return obj.text[:50] + "..." if len(obj.text) > 50 else obj.text
    text_preview.short_description = 'Comment'
