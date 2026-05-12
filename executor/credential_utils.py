"""
Credential loading helpers for workflow execution.

All running workflows use the "pre-injection" model: credentials referenced
by a workflow are decrypted ahead of time and passed into the execution
context. Nodes never decrypt at runtime.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Set

from compiler.config_access import get_credential_ref, get_node_config
from credentials.models import Credential

logger = logging.getLogger(__name__)


def get_workflow_credentials(user_id: int, workflow_json: dict) -> Dict[str, Any]:
    """
    Decrypt only the credentials referenced by nodes in this workflow.

    Least-privilege: avoids loading every active credential for the user.
    Returns a dict keyed by several aliases so nodes can look up credentials
    by ID, the legacy `cred_<id>` alias, or service identifier.
    """
    referenced_ids: Set[str] = {
        cred_id
        for node in workflow_json.get("nodes", []) or []
        if (cred_id := get_credential_ref(get_node_config(node)))
    }

    numeric_ids = [rid for rid in referenced_ids if rid.isdigit()]
    if not numeric_ids:
        return {}

    credentials = Credential.objects.filter(
        user_id=user_id,
        id__in=numeric_ids,
        is_active=True,
    ).select_related("credential_type")

    result: Dict[str, Any] = {}
    for cred in credentials:
        try:
            data = cred.get_credential_data()
        except Exception as e:
            logger.error(f"Failed to decrypt referenced credential {cred.id}: {e}")
            continue

        # Mark injection so downstream callers can audit without re-decrypting.
        if isinstance(data, dict):
            data["_injected"] = True

        sid = str(cred.id)
        result[sid] = data
        result[f"cred_{sid}"] = data
        if cred.credential_type.service_identifier:
            result[cred.credential_type.service_identifier] = data

    return result
