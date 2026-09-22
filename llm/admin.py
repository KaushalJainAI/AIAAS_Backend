from django.contrib import admin

from .models import AIModel, AIProvider, ModelFallback, ModelFallbackNotice


@admin.register(AIProvider)
class AIProviderAdmin(admin.ModelAdmin):
    list_display = ['name', 'slug', 'is_active', 'updated_at']
    list_filter = ['is_active']
    search_fields = ['name', 'slug']
    ordering = ['name']


@admin.register(AIModel)
class AIModelAdmin(admin.ModelAdmin):
    list_display = ['name', 'provider', 'value', 'is_active', 'is_free', 'source', 'input_price_per_million', 'output_price_per_million', 'context_window']
    list_filter = ['provider', 'is_active', 'is_free', 'source', 'supports_tool_calling']
    search_fields = ['name', 'value']
    ordering = ['provider', 'name']
    list_editable = ['input_price_per_million', 'output_price_per_million']
    readonly_fields = ['last_seen_at', 'retired_at']


@admin.register(ModelFallback)
class ModelFallbackAdmin(admin.ModelAdmin):
    list_display = ['provider', 'model', 'updated_by', 'updated_at']


@admin.register(ModelFallbackNotice)
class ModelFallbackNoticeAdmin(admin.ModelAdmin):
    list_display = ['subagent', 'old_value', 'created_at']
    search_fields = ['old_value']
    ordering = ['-created_at']
