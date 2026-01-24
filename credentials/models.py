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
    
    # Public credential data (not encrypted)
    public_metadata = models.JSONField(
        default=dict,
        blank=True,
        help_text='Public visibility credential data (username, urls, etc)'
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
        """Get encryption key from settings"""
        return settings.CREDENTIAL_ENCRYPTION_KEY

    def encrypt_data(self, data: dict) -> bytes:
        """Encrypt credential data"""
        import json
        fernet = Fernet(self._get_encryption_key())
        return fernet.encrypt(json.dumps(data).encode())

    def decrypt_data(self, user=None, ip_address=None, user_agent=None, workflow_id=None) -> dict:
        """
        Decrypt credential data.
        Optionally logs access if user/context is provided.
        """
        import json
        import logging
        from django.utils import timezone
        
        logger = logging.getLogger(__name__)
        
        # Audit Logging
        if user:
            try:
                CredentialAuditLog.objects.create(
                    credential=self,
                    user=user,
                    workflow_id=workflow_id,
                    action='accessed',
                    ip_address=ip_address,
                    user_agent=user_agent
                )
            except Exception as e:
                logger.error(f"Failed to create audit log: {e}")
        
        try:
            fernet = Fernet(self._get_encryption_key())
            decrypted = fernet.decrypt(self.encrypted_data)
            return json.loads(decrypted.decode())
        except Exception as e:
            # Don't expose original exception message (key error, padding, etc) in logs if it contains sensitive info
            # Only log the type of error and credential ID
            logger.error(f"Decryption failed for credential {self.id}: {type(e).__name__}")
            raise ValueError("Failed to decrypt credential data")

    def set_credential_data(self, data: dict):
        """Set and encrypt credential data"""
        self.encrypted_data = self.encrypt_data(data)

    def get_credential_data(self, **kwargs) -> dict:
        """Get decrypted credential data"""
        return self.decrypt_data(**kwargs)

    def is_token_expired(self) -> bool:
        """Check if access token is expired (with buffer)"""
        if not self.token_expires_at:
            return False 
        from django.utils import timezone
        # 5 minute buffer
        return timezone.now() > (self.token_expires_at - timezone.timedelta(minutes=5))

    def get_valid_access_token(self):
        """
        Get valid access token, refreshing if necessary.
        """
        from datetime import timedelta
        from django.utils import timezone
        import requests
        import logging
        
        logger = logging.getLogger(__name__)

        # 1. Decrypt current token
        try:
            fernet = Fernet(self._get_encryption_key())
            if self.access_token:
                current_token = fernet.decrypt(self.access_token).decode()
            else:
                return None
        except Exception:
            return None

        # 2. Check expiry
        if not self.is_token_expired():
            return current_token
            
        # 3. Refresh if expired
        if not self.refresh_token:
            logger.warning(f"Credential {self.id} expired and has no refresh token")
            return None
            
        try:
            refresh_token = fernet.decrypt(self.refresh_token).decode()
        except:
            return None
            
        config = self.credential_type.oauth_config
        token_url = config.get('token_url', 'https://oauth2.googleapis.com/token') # Default to Google for now
        client_id = settings.GOOGLE_OAUTH_CLIENT_ID
        client_secret = settings.GOOGLE_OAUTH_CLIENT_SECRET
        
        try:
            resp = requests.post(token_url, data={
                'client_id': client_id,
                'client_secret': client_secret,
                'refresh_token': refresh_token,
                'grant_type': 'refresh_token'
            }, timeout=10)
            
            if resp.status_code != 200:
                logger.error(f"Token refresh failed: {resp.text}")
                self.last_error = f"Token refresh failed: {resp.status_code}"
                self.is_verified = False
                self.save()
                return None
                
            new_tokens = resp.json()
            new_access = new_tokens.get('access_token')
            expires_in = new_tokens.get('expires_in')
            
            if new_access:
                self.access_token = fernet.encrypt(new_access.encode())
                if expires_in:
                    self.token_expires_at = timezone.now() + timedelta(seconds=expires_in)
                self.is_verified = True
                self.last_error = ""
                self.save()
                
                # Log audit
                CredentialAuditLog.objects.create(
                    credential=self,
                    user=self.user,
                    action='updated', # Refreshed
                    user_agent='System/AutoRefresh'
                )
                return new_access
                
        except Exception as e:
            logger.error(f"Token refresh error: {e}")
            
        return None


class CredentialAuditLog(models.Model):
    """
    Audit log for credential access/usage.
    """
    ACTION_CHOICES = [
        ('accessed', 'Accessed'),
        ('updated', 'Updated'),
        ('deleted', 'Deleted'),
    ]
    
    credential = models.ForeignKey(
        Credential,
        on_delete=models.SET_NULL,
        null=True,
        related_name='audit_logs'
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name='credential_audit_logs'
    )
    workflow_id = models.CharField(
        max_length=100,
        blank=True,
        null=True,
        help_text='Workflow using the credential'
    )
    action = models.CharField(
        max_length=20,
        choices=ACTION_CHOICES
    )
    timestamp = models.DateTimeField(auto_now_add=True)
    ip_address = models.GenericIPAddressField(
        blank=True,
        null=True
    )
    user_agent = models.TextField(
        blank=True,
        help_text='User Agent string'
    )

    class Meta:
        ordering = ['-timestamp']
        indexes = [
            models.Index(fields=['credential', 'timestamp']),
            models.Index(fields=['user', 'timestamp']),
        ]

    def __str__(self):
        return f"{self.action} by {self.user} at {self.timestamp}"

