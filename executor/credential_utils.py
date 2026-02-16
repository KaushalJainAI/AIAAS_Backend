from typing import Any, Dict
from credentials.models import Credential

def get_user_credentials(user_id: int) -> Dict[str, Any]:
    """
    Fetch and decrypt all active credentials for a user.
    Returns a dictionary mapping credential IDs and service identifiers
    to their decrypted data.
    
    Structure:
    {
        "cred_123": { ...decrypted data... },
        "openai": { ...decrypted data for openai credential... }, 
        # Note: If multiple credentials exist for a service, we might need a strategy.
        # For now, we'll map by ID always, and maybe by service_identifier if unique/primary.
    }
    """
    credentials = Credential.objects.filter(
        user_id=user_id, 
        is_active=True
    ).select_related('credential_type')
    
    result = {}
    
    for cred in credentials:
        try:
            # Decrypt data
            data = cred.get_credential_data()
            
            # Map by ID (always safe)
            result[str(cred.id)] = data
            result[f"cred_{cred.id}"] = data # Alias for easier access if needed
            
            # Map by service identifier if available
            # Caution: If user has multiple OpenAI keys, this will overwrite.
            # Strategy: Use the most recently created one (default ordering) or verified one.
            if cred.credential_type.service_identifier:
                # We prioritize verified credentials, then most recent
                existing = result.get(cred.credential_type.service_identifier)
                if not existing or (cred.is_verified and not existing.get('_is_verified')):
                    # Inject metadata so we know source
                    data['_source_cred_id'] = cred.id
                    data['_is_verified'] = cred.is_verified
                    result[cred.credential_type.service_identifier] = data
                    
        except Exception as e:
            # Log error but don't fail entire execution
            print(f"Failed to decrypt credential {cred.id}: {e}")
            continue
            
    return result
