from django.contrib import admin
from .models import ExecutionLog, NodeExecutionLog, AuditEntry


class NodeExecutionLogInline(admin.TabularInline):
    model = NodeExecutionLog
    fk_name = 'execution'
    extra = 0
    readonly_fields = ['node_id', 'node_type', 'node_name', 'status', 
                       'execution_order', 'started_at', 'completed_at', 'duration_ms']
    fields = ['execution_order', 'node_id', 'node_type', 'status', 'duration_ms']
    ordering = ['execution_order']
    can_delete = False
    
    def has_add_permission(self, request, obj=None):
        return False


@admin.register(ExecutionLog)
class ExecutionLogAdmin(admin.ModelAdmin):
    list_display = ['execution_id', 'workflow', 'user', 'status', 'trigger_type',
                    'nodes_executed', 'duration_ms', 'created_at']
    list_filter = ['status', 'trigger_type', 'created_at']
    search_fields = ['execution_id', 'workflow__name', 'user__username', 'user__email']
    readonly_fields = ['execution_id', 'started_at', 'completed_at', 'duration_ms',
                       'nodes_executed', 'tokens_used', 'credits_used', 
                       'created_at', 'updated_at']
    date_hierarchy = 'created_at'
    ordering = ['-created_at']
    inlines = [NodeExecutionLogInline]
    
    fieldsets = (
        ('Execution Info', {
            'fields': ('execution_id', 'workflow', 'user', 'status', 'trigger_type')
        }),
        ('Timing', {
            'fields': ('started_at', 'completed_at', 'duration_ms')
        }),
        ('Data', {
            'fields': ('input_data', 'output_data'),
            'classes': ('collapse',)
        }),
        ('Error', {
            'fields': ('error_message', 'error_node_id'),
            'classes': ('collapse',)
        }),
        ('Resource Usage', {
            'fields': ('nodes_executed', 'tokens_used', 'credits_used')
        }),
        ('Timestamps', {
            'fields': ('created_at', 'updated_at'),
            'classes': ('collapse',)
        }),
    )


@admin.register(NodeExecutionLog)
class NodeExecutionLogAdmin(admin.ModelAdmin):
    list_display = ['node_name', 'node_type', 'execution', 'status', 
                    'execution_order', 'duration_ms', 'retry_count']
    list_filter = ['status', 'node_type', 'created_at']
    search_fields = ['node_id', 'node_name', 'node_type', 'execution__execution_id']
    readonly_fields = ['created_at']
    ordering = ['-created_at', 'execution_order']
    
    fieldsets = (
        ('Node Info', {
            'fields': ('execution', 'node_id', 'node_type', 'node_name')
        }),
        ('Execution', {
            'fields': ('status', 'execution_order', 'retry_count')
        }),
        ('Timing', {
            'fields': ('started_at', 'completed_at', 'duration_ms')
        }),
        ('Data', {
            'fields': ('input_data', 'output_data', 'config'),
            'classes': ('collapse',)
        }),
        ('Error', {
            'fields': ('error_message', 'error_stack'),
            'classes': ('collapse',)
        }),
    )


@admin.register(AuditEntry)
class AuditEntryAdmin(admin.ModelAdmin):
    list_display = ['action_type', 'user', 'workflow', 'response_time_ms', 
                    'ip_address', 'created_at']
    list_filter = ['action_type', 'created_at']
    search_fields = ['user__username', 'user__email', 'workflow__name', 
                     'node_id', 'ip_address']
    readonly_fields = ['created_at']
    date_hierarchy = 'created_at'
    ordering = ['-created_at']
    
    fieldsets = (
        ('Context', {
            'fields': ('user', 'workflow', 'execution', 'node_id')
        }),
        ('Action', {
            'fields': ('action_type', 'request_details', 'response', 'response_time_ms')
        }),
        ('Metadata', {
            'fields': ('ip_address', 'user_agent'),
            'classes': ('collapse',)
        }),
        ('Timestamp', {
            'fields': ('created_at',)
        }),
    )
