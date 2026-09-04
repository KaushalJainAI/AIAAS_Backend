from django.contrib import admin
from .models import ChatSession, ChatMessage, ChatAttachment, VisionExchange, ToolOutput, ToolPermission


class ChatMessageInline(admin.TabularInline):
    model = ChatMessage
    extra = 0
    readonly_fields = ['role', 'content', 'message_type', 'created_at']
    fields = ['role', 'content', 'message_type', 'created_at']
    ordering = ['created_at']
    can_delete = False

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(ChatSession)
class ChatSessionAdmin(admin.ModelAdmin):
    list_display = ['title', 'user', 'llm_provider', 'llm_model', 'intent',
                    'memory_enabled', 'total_tokens_used', 'updated_at']
    list_filter = ['llm_provider', 'intent', 'memory_enabled', 'created_at', 'updated_at']
    search_fields = ['title', 'user__username', 'user__email', 'llm_model']
    readonly_fields = ['id', 'total_tokens_used', 'created_at', 'updated_at']
    list_editable = ['memory_enabled']
    ordering = ['-updated_at']
    inlines = [ChatMessageInline]

    fieldsets = (
        ('Session Info', {
            'fields': ('id', 'user', 'title')
        }),
        ('AI Settings', {
            'fields': ('llm_provider', 'llm_model', 'intent', 'system_prompt')
        }),
        ('Behavior', {
            'fields': ('memory_enabled',)
        }),
        ('Statistics', {
            'fields': ('total_tokens_used',)
        }),
        ('Timestamps', {
            'fields': ('created_at', 'updated_at'),
            'classes': ('collapse',)
        }),
    )


@admin.register(ChatMessage)
class ChatMessageAdmin(admin.ModelAdmin):
    list_display = ['session', 'role', 'content_preview', 'message_type', 'created_at']
    list_filter = ['role', 'message_type', 'created_at']
    search_fields = ['session__title', 'session__id', 'content']
    readonly_fields = ['created_at']
    ordering = ['-created_at']

    fieldsets = (
        ('Context', {
            'fields': ('session', 'role', 'message_type')
        }),
        ('Message', {
            'fields': ('content',)
        }),
        ('Metadata', {
            'fields': ('metadata', 'created_at'),
            'classes': ('collapse',)
        }),
    )

    @admin.display(description='Content')
    def content_preview(self, obj):
        if len(obj.content) > 50:
            return obj.content[:50] + "..."
        return obj.content


@admin.register(ChatAttachment)
class ChatAttachmentAdmin(admin.ModelAdmin):
    list_display = ['filename', 'session', 'message', 'file_type', 'file_size',
                    'is_large_file', 'created_at']
    list_filter = ['file_type', 'is_large_file', 'created_at']
    search_fields = ['filename', 'session__title', 'session__id']
    readonly_fields = ['id', 'file_size', 'created_at']
    ordering = ['-created_at']

    fieldsets = (
        ('Context', {
            'fields': ('id', 'session', 'message')
        }),
        ('File', {
            'fields': ('file', 'filename', 'file_type', 'file_size')
        }),
        ('Extraction', {
            'fields': ('extracted_text', 'inference_document', 'is_large_file')
        }),
        ('Timestamps', {
            'fields': ('created_at',),
            'classes': ('collapse',)
        }),
    )


@admin.register(VisionExchange)
class VisionExchangeAdmin(admin.ModelAdmin):
    list_display = ['session', 'attachment', 'question_preview', 'model',
                    'disagreement', 'created_at']
    list_filter = ['disagreement', 'model', 'created_at']
    search_fields = ['session__title', 'session__id', 'question', 'answer']
    readonly_fields = ['created_at']
    ordering = ['-created_at']

    fieldsets = (
        ('Context', {
            'fields': ('session', 'attachment', 'model')
        }),
        ('Exchange', {
            'fields': ('question', 'answer', 'disagreement')
        }),
        ('Timestamps', {
            'fields': ('created_at',),
            'classes': ('collapse',)
        }),
    )

    @admin.display(description='Question')
    def question_preview(self, obj):
        if len(obj.question) > 50:
            return obj.question[:50] + "..."
        return obj.question


@admin.register(ToolOutput)
class ToolOutputAdmin(admin.ModelAdmin):
    list_display = ['id', 'tool_name', 'user', 'session_key', 'total_chars',
                    'expires_at', 'created_at']
    list_filter = ['tool_name', 'created_at']
    search_fields = ['id', 'tool_name', 'session_key', 'user__username', 'user__email']
    readonly_fields = ['id', 'created_at']
    ordering = ['-created_at']

    fieldsets = (
        ('Scope', {
            'fields': ('id', 'user', 'session_key', 'turn_id')
        }),
        ('Tool Result', {
            'fields': ('tool_name', 'content', 'total_chars')
        }),
        ('Retention', {
            'fields': ('expires_at',)
        }),
        ('Timestamps', {
            'fields': ('created_at',),
            'classes': ('collapse',)
        }),
    )


@admin.register(ToolPermission)
class ToolPermissionAdmin(admin.ModelAdmin):
    list_display = ['user', 'tool_name', 'session_key', 'created_at']
    list_filter = ['created_at']
    search_fields = ['user__username', 'user__email', 'tool_name', 'session_key']
    readonly_fields = ['created_at']
    ordering = ['-created_at']
