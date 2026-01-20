from django.db import models
from django.conf import settings
from cryptography.fernet import Fernet
from django.conf import settings as django_settings
import base64
import os


class CredentialType(models.Model):
    """
    Defines types of credentials supported by the platform.
    Each type has its own schema for required fields.
    """
    AUTH_METHOD_CHOICES = [
        ('api_key', 'API Key'),
        ('oauth2', 'OAuth 2.0'),
        ('basic', 'Basic Auth'),
        ('bearer', 'Bearer Token'),
        ('custom', 'Custom'),
    ]
    
    name = models.CharField(
        max_length=100,
        unique=True,
        help_text='Display name (e.g., OpenAI, Gmail, Slack)'
    )
    slug = models.SlugField(
        max_length=100,
        unique=True,
        help_text='Unique identifier for this credential type'
    )
    description = models.TextField(
        blank=True,
        help_text='Description of what this credential is for'
    )
    icon = models.CharField(
        max_length=50,
        blank=True,
        help_text='Icon class or emoji'
    )
    auth_method = models.CharField(
        max_length=20,
        choices=AUTH_METHOD_CHOICES,
        default='api_key'
    )
    
    # Schema for credential fields
    fields_schema = models.JSONField(
        default=list,
        help_text='JSON schema defining required credential fields'
    )
    
    # OAuth Configuration (if applicable)
    oauth_config = models.JSONField(
        default=dict,
        blank=True,
        help_text='OAuth configuration (auth URL, token URL, scopes, etc.)'
    )
    
    # Status
    is_active = models.BooleanField(default=True)
    
    # Timestamps
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'Credential Type'
        verbose_name_plural = 'Credential Types'
        ordering = ['name']
        indexes = [
            models.Index(fields=['slug']),
            models.Index(fields=['auth_method', 'is_active']),
        ]

    def __str__(self):
        return self.name


class Credential(models.Model):
    """
    Stores encrypted user credentials for integrations.
    Credentials are encrypted at rest using Fernet symmetric encryption.
    """
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='credentials'
    )
    credential_type = models.ForeignKey(
        CredentialType,
        on_delete=models.PROTECT,
        related_name='credentials'
    )
    name = models.CharField(
        max_length=100,
        help_text='Friendly name for this credential'
    )
    
    # Encrypted credential data
    encrypted_data = models.BinaryField(
        help_text='Encrypted credential data'
    )
    
    # OAuth tokens (encrypted)
    access_token = models.BinaryField(
        blank=True,
        null=True,
        help_text='Encrypted OAuth access token'
    )
    refresh_token = models.BinaryField(
        blank=True,
        null=True,
        help_text='Encrypted OAuth refresh token'
    )
    token_expires_at = models.DateTimeField(
        blank=True,
        null=True,
        help_text='When the access token expires'
    )
    
    # Status
    is_active = models.BooleanField(default=True)
    is_verified = models.BooleanField(
        default=False,
        help_text='Whether the credential has been verified to work'
    )
    last_used_at = models.DateTimeField(
        blank=True,
        null=True
    )
    last_error = models.TextField(
        blank=True,
        help_text='Last error message if credential failed'
    )
    
    # Timestamps
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'Credential'
        verbose_name_plural = 'Credentials'
        ordering = ['-created_at']
        unique_together = ['user', 'name']
        indexes = [
            models.Index(fields=['user', 'credential_type']),
            models.Index(fields=['user', 'is_active']),
            models.Index(fields=['credential_type', 'is_active']),
        ]

    def __str__(self):
        return f"{self.name} ({self.credential_type.name})"

    @staticmethod
    def _get_encryption_key():
        """Get or generate encryption key from settings"""
        # In production, this should come from environment variable
        key = getattr(django_settings, 'CREDENTIAL_ENCRYPTION_KEY', None)
        if not key:
            # Generate a default key for development (NOT for production!)
            key = base64.urlsafe_b64encode(os.urandom(32))
        return key

    def encrypt_data(self, data: dict) -> bytes:
        """Encrypt credential data"""
        import json
        fernet = Fernet(self._get_encryption_key())
        return fernet.encrypt(json.dumps(data).encode())

    def decrypt_data(self) -> dict:
        """Decrypt credential data"""
        import json
        fernet = Fernet(self._get_encryption_key())
        decrypted = fernet.decrypt(self.encrypted_data)
        return json.loads(decrypted.decode())

    def set_credential_data(self, data: dict):
        """Set and encrypt credential data"""
        self.encrypted_data = self.encrypt_data(data)

    def get_credential_data(self) -> dict:
        """Get decrypted credential data"""
        return self.decrypt_data()
