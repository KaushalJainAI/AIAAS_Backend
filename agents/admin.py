from django.contrib import admin

from .models import ConversationMessage, HITLRequest, SubAgent, Trigger


@admin.register(SubAgent)
class SubAgentAdmin(admin.ModelAdmin):
    list_display = ['name', 'user', 'status', 'llm_provider', 'allow_unattended',
                    'execution_count', 'last_executed_at', 'updated_at']
    list_filter = ['status', 'allow_unattended', 'llm_provider', 'updated_at']
    search_fields = ['name', 'description', 'user__username', 'user__email']
    prepopulated_fields = {'slug': ('name',)}
    readonly_fields = ['execution_count', 'last_executed_at', 'created_at',
                       'updated_at']
    ordering = ['-updated_at']

    fieldsets = (
        ('Identity', {'fields': ('user', 'name', 'slug', 'description', 'prompt')}),
        ('Model', {'fields': ('llm_provider', 'llm_model', 'llm_credential')}),
        # Grouped the way the permissions screen renders them, so a reviewer
        # reads the same shape the installer was shown.
        ('Capability', {'fields': ('tool_grants', 'agent_context', 'guardrails',
                                   'requirements', 'sandbox')}),
        ('Result shape', {'fields': ('output_schema', 'fanout')}),
        ('Invocation', {'fields': ('allow_unattended', 'status', 'runtime_settings')}),
        ('Appearance', {'fields': ('icon', 'color', 'tags', 'is_template')}),
        ('Statistics', {'fields': ('execution_count', 'last_executed_at')}),
        ('Timestamps', {'fields': ('created_at', 'updated_at'),
                        'classes': ('collapse',)}),
    )


@admin.register(Trigger)
class TriggerAdmin(admin.ModelAdmin):
    list_display = ['subagent', 'mode', 'enabled', 'next_due_at',
                    'last_fired_at', 'consecutive_failures']
    list_filter = ['mode', 'enabled']
    search_fields = ['subagent__name', 'subagent__user__username']
    # Never editable and never listed: it is the whole credential on the public
    # webhook route.
    readonly_fields = ['secret', 'last_fired_at', 'next_due_at',
                       'consecutive_failures', 'created_at', 'updated_at']
    ordering = ['-updated_at']


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
                    'subagent', 'created_at']
    list_filter = ['role', 'created_at']
    search_fields = ['conversation_id', 'content', 'user__username']
    readonly_fields = ['created_at']
    ordering = ['-created_at']
    
    fieldsets = (
        ('Context', {
            'fields': ('user', 'conversation_id', 'subagent')
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
