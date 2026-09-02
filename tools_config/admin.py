from django.contrib import admin

from .models import ToolConfig


@admin.register(ToolConfig)
class ToolConfigAdmin(admin.ModelAdmin):
    list_display = ('tool_name', 'user', 'enabled', 'updated_at')
    list_filter = ('enabled',)
    search_fields = ('tool_name', 'user__username', 'user__email')
