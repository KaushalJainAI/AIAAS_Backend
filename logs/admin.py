from django.contrib import admin

from .models import AgentStep, AgentTurn, ExecutionLog, SubAgentRevision


class AgentStepInline(admin.TabularInline):
    model = AgentStep
    fk_name = 'turn'
    extra = 0
    readonly_fields = ['call_id', 'tool', 'status', 'order', 'duration_ms']
    fields = ['order', 'tool', 'call_id', 'status', 'duration_ms']
    ordering = ['order']
    can_delete = False

    def has_add_permission(self, request, obj=None):
        return False


class AgentTurnInline(admin.TabularInline):
    model = AgentTurn
    fk_name = 'execution'
    extra = 0
    readonly_fields = ['index', 'decision', 'model_id', 'tokens', 'duration_ms']
    fields = ['index', 'decision', 'model_id', 'tokens', 'duration_ms']
    ordering = ['index']
    can_delete = False
    show_change_link = True

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(ExecutionLog)
class ExecutionLogAdmin(admin.ModelAdmin):
    list_display = ['execution_id', 'subagent', 'user', 'status', 'caller',
                    'trigger_type', 'depth', 'duration_ms', 'created_at']
    list_filter = ['status', 'caller', 'trigger_type', 'created_at']
    search_fields = ['execution_id', 'subagent__name', 'user__username', 'user__email']
    readonly_fields = ['execution_id', 'started_at', 'completed_at', 'duration_ms',
                       'nodes_executed', 'tokens_used', 'credits_used',
                       'created_at', 'updated_at']
    raw_id_fields = ['parent_step', 'revision', 'subagent']
    date_hierarchy = 'created_at'
    ordering = ['-created_at']
    inlines = [AgentTurnInline]

    fieldsets = (
        ('Run', {
            'fields': ('execution_id', 'subagent', 'revision', 'user',
                       'status', 'caller', 'trigger_type')
        }),
        ('Delegation', {
            'fields': ('parent_step', 'delegation_task', 'delegation_index', 'depth'),
            'classes': ('collapse',)
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


@admin.register(AgentTurn)
class AgentTurnAdmin(admin.ModelAdmin):
    list_display = ['execution', 'index', 'decision', 'provider', 'model_id',
                    'tokens', 'duration_ms', 'created_at']
    list_filter = ['decision', 'provider', 'created_at']
    search_fields = ['execution__execution_id', 'reasoning', 'content', 'model_id']
    readonly_fields = ['created_at']
    raw_id_fields = ['execution']
    ordering = ['-created_at']
    inlines = [AgentStepInline]

    fieldsets = (
        ('Turn', {'fields': ('execution', 'index', 'decision')}),
        ('Model', {'fields': ('provider', 'model_id', 'tokens', 'duration_ms')}),
        ('Reasoning', {'fields': ('reasoning', 'reasoning_truncated')}),
        ('Content', {'fields': ('content', 'content_truncated'),
                     'classes': ('collapse',)}),
        ('Timestamp', {'fields': ('created_at',)}),
    )


@admin.register(AgentStep)
class AgentStepAdmin(admin.ModelAdmin):
    list_display = ['tool', 'execution', 'turn', 'status', 'order', 'duration_ms']
    list_filter = ['status', 'tool', 'created_at']
    search_fields = ['call_id', 'tool', 'execution__execution_id']
    readonly_fields = ['created_at']
    raw_id_fields = ['execution', 'turn']
    ordering = ['-created_at', 'order']

    fieldsets = (
        ('Step', {'fields': ('execution', 'turn', 'call_id', 'tool')}),
        ('Execution', {'fields': ('status', 'order')}),
        ('Timing', {'fields': ('started_at', 'completed_at', 'duration_ms')}),
        ('Data', {'fields': ('args', 'result'), 'classes': ('collapse',)}),
        ('Error', {'fields': ('error_message',), 'classes': ('collapse',)}),
    )


@admin.register(SubAgentRevision)
class SubAgentRevisionAdmin(admin.ModelAdmin):
    list_display = ['subagent', 'number', 'summary', 'source', 'user', 'created_at']
    list_filter = ['source', 'created_at']
    search_fields = ['subagent__name', 'summary']
    readonly_fields = ['created_at']
    raw_id_fields = ['subagent', 'user']
    ordering = ['-created_at']

    fieldsets = (
        ('Revision', {'fields': ('subagent', 'number', 'source', 'user', 'summary')}),
        ('Change', {'fields': ('diff',)}),
        ('Snapshot', {'fields': ('config',), 'classes': ('collapse',)}),
        ('Timestamp', {'fields': ('created_at',)}),
    )
