from django.contrib import admin
from .models import Workflow, WorkflowVersion, HITLRequest, ConversationMessage


class WorkflowVersionInline(admin.TabularInline):
    model = WorkflowVersion
    extra = 0
    readonly_fields = ['version_number', 'label', 'created_by', 'change_summary', 'created_at']
    fields = ['version_number', 'label', 'change_summary', 'created_at']
    ordering = ['-version_number']
    can_delete = False
    max_num = 10
    
    def has_add_permission(self, request, obj=None):
        return False


@admin.register(Workflow)
class WorkflowAdmin(admin.ModelAdmin):
    list_display = ['name', 'user', 'status', 'is_template', 'node_count',
                    'execution_count', 'last_executed_at', 'updated_at']
    list_filter = ['status', 'is_template', 'created_at', 'updated_at']
    search_fields = ['name', 'description', 'user__username', 'user__email']
    prepopulated_fields = {'slug': ('name',)}
    list_editable = ['status']
    readonly_fields = ['execution_count', 'last_executed_at', 'created_at', 'updated_at']
    ordering = ['-updated_at']
    inlines = [WorkflowVersionInline]
    
    fieldsets = (
        ('Identity', {
            'fields': ('user', 'name', 'slug', 'description')
        }),
        ('Workflow Definition', {
            'fields': ('nodes', 'edges', 'viewport', 'workflow_settings'),
            'classes': ('collapse',)
        }),
        ('Status & Type', {
            'fields': ('status', 'is_template')
        }),
        ('Appearance', {
            'fields': ('icon', 'color', 'tags')
        }),
        ('Statistics', {
            'fields': ('execution_count', 'last_executed_at')
        }),
        ('Timestamps', {
            'fields': ('created_at', 'updated_at'),
            'classes': ('collapse',)
        }),
    )
    
    def node_count(self, obj):
        return obj.node_count
    node_count.short_description = 'Nodes'


@admin.register(WorkflowVersion)
class WorkflowVersionAdmin(admin.ModelAdmin):
    list_display = ['workflow', 'version_number', 'label', 'created_by', 'created_at']
    list_filter = ['created_at']
    search_fields = ['workflow__name', 'label', 'change_summary']
    readonly_fields = ['created_at']
    ordering = ['-created_at']
    
    fieldsets = (
        ('Version Info', {
            'fields': ('workflow', 'version_number', 'label', 'change_summary')
        }),
        ('Snapshot', {
            'fields': ('nodes', 'edges', 'workflow_settings'),
            'classes': ('collapse',)
        }),
        ('Metadata', {
            'fields': ('created_by', 'created_at')
        }),
    )


@admin.register(HITLRequest)
class HITLRequestAdmin(admin.ModelAdmin):
    list_display = ['request_id', 'request_type', 'title', 'user', 'status',
                    'timeout_seconds', 'created_at', 'responded_at']
    list_filter = ['request_type', 'status', 'created_at']
    search_fields = ['request_id', 'title', 'message', 'user__username']
    readonly_fields = ['request_id', 'created_at', 'updated_at', 'responded_at']
    list_editable = ['status']
    ordering = ['-created_at']
    
    fieldsets = (
        ('Request Info', {
            'fields': ('request_id', 'execution', 'user', 'node_id')
        }),
        ('Request Details', {
            'fields': ('request_type', 'title', 'message', 'options', 'context_data')
        }),
        ('Response', {
            'fields': ('status', 'response', 'responded_at')
        }),
        ('Timeout', {
            'fields': ('timeout_seconds', 'auto_action')
        }),
        ('Timestamps', {
            'fields': ('created_at', 'updated_at'),
            'classes': ('collapse',)
        }),
    )


@admin.register(ConversationMessage)
class ConversationMessageAdmin(admin.ModelAdmin):
    list_display = ['conversation_id', 'user', 'role', 'content_preview', 
                    'workflow', 'created_at']
    list_filter = ['role', 'created_at']
    search_fields = ['conversation_id', 'content', 'user__username']
    readonly_fields = ['created_at']
    ordering = ['-created_at']
    
    fieldsets = (
        ('Context', {
            'fields': ('user', 'conversation_id', 'workflow')
        }),
        ('Message', {
            'fields': ('role', 'content')
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
