from django.contrib import admin
from .models import CustomNode


@admin.register(CustomNode)
class CustomNodeAdmin(admin.ModelAdmin):
    list_display = ['name', 'node_type', 'user', 'category', 'status', 
                    'is_validated', 'is_public', 'version', 'created_at']
    list_filter = ['category', 'status', 'is_validated', 'is_public', 'created_at']
    search_fields = ['name', 'node_type', 'description', 'user__username']
    list_editable = ['status', 'is_public']
    readonly_fields = ['is_validated', 'validation_errors', 'created_at', 'updated_at']
    ordering = ['-created_at']
    
    fieldsets = (
        ('Node Identity', {
            'fields': ('user', 'name', 'node_type', 'category', 'description')
        }),
        ('Appearance', {
            'fields': ('icon', 'color')
        }),
        ('Code & Schema', {
            'fields': ('code', 'fields_schema', 'input_schema', 'output_schema'),
            'classes': ('collapse',)
        }),
        ('Validation', {
            'fields': ('status', 'is_validated', 'validation_errors')
        }),
        ('Sharing', {
            'fields': ('is_public', 'version')
        }),
        ('Timestamps', {
            'fields': ('created_at', 'updated_at'),
            'classes': ('collapse',)
        }),
    )
