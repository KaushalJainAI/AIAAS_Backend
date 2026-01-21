"""
Credential Manager

Central service for credential operations including fetch, decrypt,
validation, and OAuth token refresh.
"""
import logging
from datetime import datetime, timedelta
from typing import Any
import httpx

from django.db import transaction
from django.utils import timezone

from .models import Credential, CredentialType

logger = logging.getLogger(__name__)


class CredentialManager:
    """
    Central service for handling credential operations.
    
    Responsibilities:
    - Fetch and decrypt credentials by ID
    - Validate credential data against type schema
    - Handle OAuth token refresh
    - Audit logging for credential access
    
    Usage:
        manager = CredentialManager()
        creds = await manager.get_credential("cred_123", user_id=1)
    """
    
    def __init__(self):
        self._cache: dict[str, tuple[dict, datetime]] = {}
        self._cache_ttl = timedelta(minutes=5)
    
    async def get_credential(
        self,
        credential_id: str | int,
        user_id: int,
        refresh_if_expired: bool = True
    ) -> dict[str, Any] | None:
        """
        Fetch and decrypt a credential.
        
        Args:
            credential_id: The credential ID or name
            user_id: User ID (for access control)
            refresh_if_expired: Auto-refresh OAuth tokens if expired
            
        Returns:
            Decrypted credential data dict, or None if not found
        """
        from asgiref.sync import sync_to_async
        
        cache_key = f"{user_id}:{credential_id}"
        
        # Check cache
        if cache_key in self._cache:
            data, cached_at = self._cache[cache_key]
            if timezone.now() - cached_at < self._cache_ttl:
                return data
        
        # Fetch from database
        try:
            credential = await sync_to_async(
                Credential.objects.select_related('credential_type').get
            )(
                id=credential_id,
                user_id=user_id,
                is_active=True
            )
        except Credential.DoesNotExist:
            logger.warning(f"Credential {credential_id} not found for user {user_id}")
            return None
        except ValueError:
            # Try by name instead
            try:
                credential = await sync_to_async(
                    Credential.objects.select_related('credential_type').get
                )(
                    name=credential_id,
                    user_id=user_id,
                    is_active=True
                )
            except Credential.DoesNotExist:
                logger.warning(f"Credential '{credential_id}' not found for user {user_id}")
                return None
        
        # Check if OAuth token refresh needed
        if (
            refresh_if_expired
            and credential.credential_type.auth_method == 'oauth2'
            and credential.token_expires_at
            and credential.token_expires_at <= timezone.now()
        ):
            await self.refresh_oauth_token(credential)
        
        # Decrypt and return
        try:
            data = credential.get_credential_data()
            
            # Add access token for OAuth credentials
            if credential.access_token:
                from cryptography.fernet import Fernet
                fernet = Fernet(credential._get_encryption_key())
                data['access_token'] = fernet.decrypt(credential.access_token).decode()
            
            # Update last used
            credential.last_used_at = timezone.now()
            await sync_to_async(credential.save)(update_fields=['last_used_at'])
            
            # Cache the result
            self._cache[cache_key] = (data, timezone.now())
            
            logger.info(f"Credential {credential_id} accessed by user {user_id}")
            return data
            
        except Exception as e:
            logger.error(f"Failed to decrypt credential {credential_id}: {e}")
            return None
    
    async def refresh_oauth_token(self, credential: Credential) -> bool:
        """
        Refresh an expired OAuth token.
        
        Args:
            credential: The Credential model instance
            
        Returns:
            True if refresh succeeded
        """
        from asgiref.sync import sync_to_async
        from cryptography.fernet import Fernet
        
        if not credential.refresh_token:
            logger.warning(f"No refresh token for credential {credential.id}")
            return False
        
        oauth_config = credential.credential_type.oauth_config
        if not oauth_config.get('token_url'):
            logger.error(f"No token URL configured for {credential.credential_type.name}")
            return False
        
        try:
            # Decrypt refresh token
            fernet = Fernet(credential._get_encryption_key())
            refresh_token = fernet.decrypt(credential.refresh_token).decode()
            
            # Get client credentials from the credential data
            cred_data = credential.get_credential_data()
            client_id = cred_data.get('client_id') or oauth_config.get('client_id')
            client_secret = cred_data.get('client_secret') or oauth_config.get('client_secret')
            
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    oauth_config['token_url'],
                    data={
                        'grant_type': 'refresh_token',
                        'refresh_token': refresh_token,
                        'client_id': client_id,
                        'client_secret': client_secret,
                    },
                )
                
                if response.status_code != 200:
                    error_text = response.text
                    logger.error(f"OAuth refresh failed for {credential.id}: {error_text}")
                    credential.last_error = f"Token refresh failed: {error_text}"
                    await sync_to_async(credential.save)(update_fields=['last_error'])
                    return False
                
                data = response.json()
                
                # Update tokens
                new_access_token = data.get('access_token')
                new_refresh_token = data.get('refresh_token', refresh_token)
                expires_in = data.get('expires_in', 3600)
                
                credential.access_token = fernet.encrypt(new_access_token.encode())
                credential.refresh_token = fernet.encrypt(new_refresh_token.encode())
                credential.token_expires_at = timezone.now() + timedelta(seconds=expires_in)
                credential.last_error = ''
                
                await sync_to_async(credential.save)(
                    update_fields=['access_token', 'refresh_token', 'token_expires_at', 'last_error']
                )
                
                logger.info(f"OAuth token refreshed for credential {credential.id}")
                return True
                
        except Exception as e:
            logger.error(f"Failed to refresh OAuth token: {e}")
            return False
    
    def validate_against_schema(
        self,
        data: dict[str, Any],
        credential_type: CredentialType
    ) -> list[str]:
        """
        Validate credential data against type schema.
        
        Args:
            data: The credential data to validate
            credential_type: The CredentialType defining the schema
            
        Returns:
            List of validation error messages (empty if valid)
        """
        errors = []
        schema = credential_type.fields_schema
        
        for field in schema:
            field_name = field.get('name')
            required = field.get('required', True)
            field_type = field.get('type', 'string')
            
            if required and field_name not in data:
                errors.append(f"Missing required field: {field_name}")
                continue
            
            value = data.get(field_name)
            if value is None and not required:
                continue
            
            # Type validation
            if field_type == 'string' and not isinstance(value, str):
                errors.append(f"Field '{field_name}' must be a string")
            elif field_type == 'number' and not isinstance(value, (int, float)):
                errors.append(f"Field '{field_name}' must be a number")
            elif field_type == 'boolean' and not isinstance(value, bool):
                errors.append(f"Field '{field_name}' must be a boolean")
        
        return errors
    
    def clear_cache(self, user_id: int | None = None) -> None:
        """
        Clear credential cache.
        
        Args:
            user_id: If provided, only clear that user's cache
        """
        if user_id is None:
            self._cache.clear()
        else:
            keys_to_remove = [k for k in self._cache if k.startswith(f"{user_id}:")]
            for key in keys_to_remove:
                del self._cache[key]


# Global instance
_credential_manager: CredentialManager | None = None


def get_credential_manager() -> CredentialManager:
    """Get the global CredentialManager instance."""
    global _credential_manager
    if _credential_manager is None:
        _credential_manager = CredentialManager()
    return _credential_manager
