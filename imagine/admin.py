from django.contrib import admin

from .models import Generation, ImagineConversation, ImagineMessage


@admin.register(Generation)
class GenerationAdmin(admin.ModelAdmin):
    list_display = ['id', 'user', 'type', 'model', 'status', 'created_at']
    list_filter = ['type', 'status', 'created_at']
    search_fields = ['prompt', 'model', 'user__username', 'user__email']
    readonly_fields = ['output_url', 'job_id', 'polling_url', 'metadata',
                       'created_at', 'updated_at']
    raw_id_fields = ['user']
    ordering = ['-created_at']


@admin.register(ImagineConversation)
class ImagineConversationAdmin(admin.ModelAdmin):
    list_display = ['id', 'user', 'title', 'status', 'updated_at']
    list_filter = ['status', 'created_at']
    search_fields = ['title', 'user__username', 'user__email']
    raw_id_fields = ['user']
    ordering = ['-updated_at']


@admin.register(ImagineMessage)
class ImagineMessageAdmin(admin.ModelAdmin):
    list_display = ['id', 'conversation', 'role', 'requires_hitl', 'created_at']
    list_filter = ['role', 'requires_hitl', 'created_at']
    search_fields = ['content', 'conversation__title']
    raw_id_fields = ['conversation', 'generation']
    ordering = ['-created_at']