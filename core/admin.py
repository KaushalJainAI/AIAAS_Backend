from django.contrib import admin
from .models import UserProfile, APIKey, UsageTracking


@admin.register(UserProfile)
class UserProfileAdmin(admin.ModelAdmin):
    list_display = ['user', 'tier', 'credits_remaining', 'credits_used_total', 'created_at']
    list_filter = ['tier', 'created_at']
    search_fields = ['user__username', 'user__email']
    readonly_fields = ['credits_used_total', 'created_at', 'updated_at']
    ordering = ['-created_at']
    
    fieldsets = (
        ('User Link', {
            'fields': ('user',)
        }),
        ('Subscription', {
            'fields': ('tier',)
        }),
        ('Rate Limits', {
            'fields': ('compile_limit', 'execute_limit', 'stream_connections')
        }),
        ('Credits', {
            'fields': ('credits_remaining', 'credits_used_total')
        }),
        ('Timestamps', {
            'fields': ('created_at', 'updated_at'),
            'classes': ('collapse',)
        }),
    )


@admin.register(APIKey)
class APIKeyAdmin(admin.ModelAdmin):
    list_display = ['name', 'user', 'key_prefix', 'is_active', 'last_used_at', 'created_at']
    list_filter = ['is_active', 'created_at']
    search_fields = ['name', 'user__username', 'user__email', 'key_prefix']
    readonly_fields = ['key', 'key_prefix', 'last_used_at', 'created_at', 'updated_at']
    list_editable = ['is_active']
    ordering = ['-created_at']
    
    fieldsets = (
        ('Key Information', {
            'fields': ('user', 'name')
        }),
        ('Key Details', {
            'fields': ('key', 'key_prefix'),
            'description': 'API key is auto-generated and shown only once on creation'
        }),
        ('Status', {
            'fields': ('is_active', 'expires_at', 'last_used_at')
        }),
        ('Timestamps', {
            'fields': ('created_at', 'updated_at'),
            'classes': ('collapse',)
        }),
    )


@admin.register(UsageTracking)
class UsageTrackingAdmin(admin.ModelAdmin):
    list_display = ['user', 'date', 'compile_count', 'execute_count', 'chat_count', 
                    'tokens_used', 'credits_used', 'estimated_cost']
    list_filter = ['date', 'user']
    search_fields = ['user__username', 'user__email']
    readonly_fields = ['created_at', 'updated_at']
    date_hierarchy = 'date'
    ordering = ['-date']
    
    fieldsets = (
        ('User & Date', {
            'fields': ('user', 'date')
        }),
        ('Request Counts', {
            'fields': ('compile_count', 'execute_count', 'chat_count')
        }),
        ('Usage Metrics', {
            'fields': ('tokens_used', 'credits_used', 'estimated_cost')
        }),
        ('Timestamps', {
            'fields': ('created_at', 'updated_at'),
            'classes': ('collapse',)
        }),
    )
