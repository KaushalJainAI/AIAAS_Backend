from typing import Any, Dict, Set
import json
import logging
from credentials.models import Credential

logger = logging.getLogger(__name__)

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
            logger.error(f"Failed to decrypt credential {cred.id}: {e}")
            continue
            

def get_workflow_credentials(user_id: int, workflow_json: dict) -> Dict[str, Any]:
    """
    Identify and decrypt ONLY the credentials referenced in a workflow.
    This provides security by least privilege (not loading all user keys)
    while maintaining the stable 'pre-injection' model.
    """
    referenced_ids: Set[str] = set()
    
    # 1. Scan nodes for credential usage
    nodes = workflow_json.get('nodes', [])
    for node in nodes:
        data = node.get('data', {})
        # Check common credential field names in n8n-style nodes
        cred_id = data.get('credential') or data.get('credential_id') or data.get('credentialId')
        if cred_id:
            referenced_ids.add(str(cred_id))
            
    if not referenced_ids:
        return {}
        
    # 2. Fetch and decrypt only referenced ones
    credentials = Credential.objects.filter(
        user_id=user_id,
        id__in=[rid for rid in referenced_ids if rid.isdigit()],
        is_active=True
    ).select_related('credential_type')
    
    result = {}
    for cred in credentials:
        try:
            data = cred.get_credential_data()
            str_id = str(cred.id)
            result[str_id] = data
            result[f"cred_{str_id}"] = data
            
            # Also map by service identifier for nodes that look up by type
            if cred.credential_type.service_identifier:
                result[cred.credential_type.service_identifier] = data
                
            # Security: Mark as injected so we can audit if needed
            result[str_id]['_injected'] = True
            
        except Exception as e:
            logger.error(f"Failed to decrypt referenced credential {cred.id}: {e}")
            
    return result
