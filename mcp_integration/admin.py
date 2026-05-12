from django.contrib import admin

from .models import MCPServer


@admin.register(MCPServer)
class MCPServerAdmin(admin.ModelAdmin):
    list_display = ('name', 'type', 'enabled', 'user', 'updated_at')
    list_filter = ('type', 'enabled')
    search_fields = ('name',)
    readonly_fields = ('created_at', 'updated_at')

