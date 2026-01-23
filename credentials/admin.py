from django.contrib import admin
from .models import CredentialType, Credential


@admin.register(CredentialType)
class CredentialTypeAdmin(admin.ModelAdmin):
    list_display = ['name', 'slug', 'auth_method', 'is_active', 'created_at']
    list_filter = ['auth_method', 'is_active', 'created_at']
    search_fields = ['name', 'slug', 'description']
    prepopulated_fields = {'slug': ('name',)}
    list_editable = ['is_active']
    ordering = ['name']
    
    fieldsets = (
        ('Basic Information', {
            'fields': ('name', 'slug', 'description', 'icon')
        }),
        ('Authentication', {
            'fields': ('auth_method', 'fields_schema')
        }),
        ('OAuth Configuration', {
            'fields': ('oauth_config',),
            'classes': ('collapse',),
            'description': 'Only applicable for OAuth 2.0 auth method'
        }),
        ('Status', {
            'fields': ('is_active',)
        }),
    )


@admin.register(Credential)
class CredentialAdmin(admin.ModelAdmin):
    list_display = ['name', 'user', 'credential_type', 'is_active', 
                    'is_verified', 'last_used_at', 'created_at']
    list_filter = ['credential_type', 'is_active', 'is_verified', 'created_at']
    search_fields = ['name', 'user__username', 'user__email']
    list_editable = ['is_active']
    readonly_fields = ['encrypted_data', 'decrypted_data_preview', 'access_token', 'refresh_token', 
                       'last_used_at', 'created_at', 'updated_at']
    ordering = ['-created_at']
    
    def decrypted_data_preview(self, obj):
        try:
            return obj.get_credential_data()
        except Exception as e:
            return f"Error decrypting: {e}"
    decrypted_data_preview.short_description = "Decrypted Data (Preview)"

    fieldsets = (
        ('Ownership', {
            'fields': ('user', 'credential_type', 'name')
        }),
        ('Encrypted Data', {
            'fields': ('encrypted_data', 'decrypted_data_preview'),
            'classes': ('collapse',),
            'description': 'Encrypted credential data (decrypted view for admins)'
        }),
        ('OAuth Tokens', {
            'fields': ('access_token', 'refresh_token', 'token_expires_at'),
            'classes': ('collapse',)
        }),
        ('Status', {
            'fields': ('is_active', 'is_verified', 'last_used_at', 'last_error')
        }),
        ('Timestamps', {
            'fields': ('created_at', 'updated_at'),
            'classes': ('collapse',)
        }),
    )

