from django.contrib import admin
from .models import StreamEvent


@admin.register(StreamEvent)
class StreamEventAdmin(admin.ModelAdmin):
    list_display = ['event_id', 'execution', 'event_type', 'node_id', 
                    'sequence', 'created_at']
    list_filter = ['event_type', 'created_at']
    search_fields = ['event_id', 'execution__execution_id', 'node_id', 
                     'user__username']
    readonly_fields = ['event_id', 'created_at']
    ordering = ['-created_at', 'sequence']
    date_hierarchy = 'created_at'
    
    fieldsets = (
        ('Event Info', {
            'fields': ('event_id', 'execution', 'user')
        }),
        ('Event Details', {
            'fields': ('event_type', 'node_id', 'sequence')
        }),
        ('Payload', {
            'fields': ('data',)
        }),
        ('Timestamp', {
            'fields': ('created_at',)
        }),
    )
