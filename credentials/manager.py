"""
Credential Manager

Central service for credential operations including fetch, decrypt,
validation, and OAuth token refresh.
"""
import logging
from datetime import datetime, timedelta
from typing import Any

from django.conf import settings
from django.utils import timezone

from .models import Credential, CredentialType

logger = logging.getLogger(__name__)

# Maximum number of entries in the credential cache to prevent unbounded memory growth
MAX_CACHE_SIZE = 1000


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
    
    def _evict_cache(self) -> None:
        """Evict expired entries and enforce max cache size."""
        now = timezone.now()
        # Remove expired entries first
        expired_keys = [
            k for k, (_, cached_at) in self._cache.items()
            if now - cached_at >= self._cache_ttl
        ]
        for k in expired_keys:
            del self._cache[k]
        
        # If still over limit, evict oldest entries (LRU-style)
        if len(self._cache) >= MAX_CACHE_SIZE:
            sorted_keys = sorted(self._cache.keys(), key=lambda k: self._cache[k][1])
            excess_count = len(self._cache) - MAX_CACHE_SIZE + 1
            for k in sorted_keys[:excess_count]:
                del self._cache[k]

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
            else:
                # Expired — remove from cache
                del self._cache[cache_key]
        
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
        
        # Check if OAuth token refresh needed (5-minute buffer to avoid
        # returning a token that expires mid-request).
        if (
            refresh_if_expired
            and credential.credential_type.auth_method == 'oauth2'
            and credential.token_expires_at
            and credential.token_expires_at - timedelta(minutes=5) <= timezone.now()
        ):
            await self.refresh_oauth_token(credential)
            # Bust cache for this credential so we don't return the stale token below.
            self._cache.pop(cache_key, None)
        
        # Decrypt and return
        try:
            data = credential.get_credential_data()

            # Merge the dedicated token columns over the encrypted blob.
            #
            # `get_credential_data()` reads `encrypted_data` only, but the OAuth
            # flow writes nothing there — it stores tokens in these two columns.
            # Without this merge an OAuth-connected account looks *empty* to
            # every caller, which is what made a freshly connected Google
            # connection fail with "missing field 'refresh_token'".
            #
            # `refresh_token` is merged as well as `access_token` because MCP
            # connectors are handed the refresh token and renew for themselves;
            # reading only the access token would give them an hour of life.
            #
            # A value already in the blob WINS over the column: the blob is
            # where a hand-entered credential lives, and shadowing it with a
            # token column left over from an earlier OAuth connect is how a
            # credential the user just typed in stops working. Pinned by
            # `test_credential_bridge.py::test_a_blob_field_wins_over_the_column`
            # — do not "unify" this to column-wins.
            #
            # A token that cannot be decrypted is treated as absent rather than
            # fatal, so the caller reports a precise "missing field" naming what
            # to reconnect instead of a decryption stack trace.
            from cryptography.fernet import Fernet

            for field in ('access_token', 'refresh_token'):
                if data.get(field):
                    continue
                raw = getattr(credential, field, None)
                if not raw:
                    continue
                try:
                    fernet = Fernet(credential._get_encryption_key())
                    data[field] = fernet.decrypt(bytes(raw)).decode()
                except Exception:  # noqa: BLE001
                    logger.warning(
                        "Could not decrypt %s column on credential %s",
                        field, credential.id,
                    )
            
            # Update last used
            credential.last_used_at = timezone.now()
            await sync_to_async(credential.save)(update_fields=['last_used_at'])
            
            # Audit logging: always log credential access
            try:
                from .models import CredentialAuditLog
                await sync_to_async(CredentialAuditLog.objects.create)(
                    credential=credential,
                    user_id=user_id,
                    action='accessed',
                )
            except Exception as e:
                logger.error(f"Failed to create audit log for credential {credential_id}: {e}")
            
            # Cache the result (with size limit)
            self._evict_cache()
            self._cache[cache_key] = (data, timezone.now())
            
            logger.info(f"Credential {credential_id} accessed by user {user_id}")
            return data
            
        except Exception as e:
            logger.error(f"Failed to decrypt credential {credential_id}: {e}")
            return None
    
    @staticmethod
    def lookup_by_slug_sync(slug: str, user_id: int) -> Credential | None:
        """
        The user's best credential of a given type.

        Ordering is load-bearing and was moved here verbatim from
        `mcp_integration.credential_injector`: a verified credential beats an
        unverified one, and the most recently updated wins the tie. That second
        key is what makes "connect Gmail after Calendar keeps both working"
        true — Google's `include_granted_scopes=true` means the newer token
        carries the older scopes too, so the newest row is the most capable one,
        not merely the newest.
        """
        return (
            Credential.objects
            .filter(user_id=user_id, credential_type__slug=slug, is_active=True)
            .select_related("credential_type")
            .order_by("-is_verified", "-updated_at")
            .first()
        )

    async def get_credential_by_slug(
        self,
        slug: str,
        user_id: int,
        refresh_if_expired: bool = True,
    ) -> dict[str, Any] | None:
        """
        Fetch and decrypt a user's credential of a given *type*.

        `get_credential` answers "this exact credential"; callers that only know
        a type — every MCP connector, whose wiring reads `"<slug>:<field>"` —
        need this one. It resolves the row, then goes through `get_credential`
        so there is a single path for the cache, the OAuth refresh, the audit
        entry and `last_used_at`. The injector previously carried its own
        lookup and its own decrypt, which is how it ended up with neither a
        cache nor a refresh.
        """
        from asgiref.sync import sync_to_async

        credential = await sync_to_async(self.lookup_by_slug_sync)(slug, user_id)
        if credential is None:
            return None
        return await self.get_credential(
            credential.id, user_id, refresh_if_expired=refresh_if_expired
        )

    async def refresh_oauth_token(self, credential: Credential) -> bool:
        """
        Refresh an expired OAuth token. Returns True if a token is now valid.

        Delegates to `Credential.get_valid_access_token`, which is the same
        exchange done properly: it takes a `select_for_update` lock and
        re-checks expiry after acquiring it, so N concurrent callers perform one
        refresh instead of N. Google issues a new refresh token on some
        refreshes and invalidates the old one, so a lost race there does not
        just waste a request — it can leave a credential holding a token Google
        has already retired.

        This method used to be a second, lock-free implementation of the same
        exchange, which is how the two drifted: that one read `token_url` out of
        `oauth_config` and bailed when it was absent (it was, on every install
        until `credentials.0008`), while the model method defaulted the URL
        inline and worked. One exchange, one lock, one place to fix.
        """
        from asgiref.sync import sync_to_async

        if not credential.refresh_token:
            logger.warning(f"No refresh token for credential {credential.id}")
            return False

        token = await sync_to_async(credential.get_valid_access_token)()
        if token is None:
            return False

        # `get_valid_access_token` writes through a `select_for_update` re-fetch
        # (that lock is the whole point), so the *caller's* instance still holds
        # the pre-refresh column. Without this the next line to decrypt
        # `credential.access_token` hands back the token we just replaced —
        # a refresh that reports success and changes nothing the caller sees.
        await sync_to_async(credential.refresh_from_db)()
        return True

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
