"""
Canonical accessors for the ReactFlow node payload.

The frontend historically shipped node configs in two shapes:

    {"data": {"config": {...}, "label": "Foo"}}   # preferred
    {"data": {"field1": ..., "field2": ...}}      # legacy

Code across the backend kept re-implementing this fallback inline, which
drifts over time. Use these helpers instead.
"""
from __future__ import annotations

from typing import Any


def get_node_data(node: dict[str, Any]) -> dict[str, Any]:
    """Return node['data'] as a dict, never None."""
    return node.get("data") or {}


def get_node_config(node: dict[str, Any]) -> dict[str, Any]:
    """
    Return the node's config dict.

    Prefers node.data.config; falls back to node.data (legacy shape).
    Always returns a dict — never None.
    """
    data = get_node_data(node)
    cfg = data.get("config")
    if isinstance(cfg, dict):
        return cfg
    return data


def get_node_label(node: dict[str, Any]) -> str | None:
    """Return the user-facing label, or None if unset."""
    data = get_node_data(node)
    return data.get("label") or data.get("config", {}).get("label") if isinstance(data.get("config"), dict) else data.get("label")


# Credential reference keys we accept from frontend configs.
# Kept tolerant because different node families evolved different names;
# unifying them in the UI is a separate task.
_CREDENTIAL_KEYS = ("credential_id", "credentialId", "credential")


def get_credential_ref(config: dict[str, Any]) -> str | None:
    """
    Extract a credential ID reference from a node config.

    Returns a string (credential ID) or None if the node does not reference one.
    Does NOT validate that the credential exists — that's validate_credentials's job.
    """
    for key in _CREDENTIAL_KEYS:
        val = config.get(key)
        if val:
            return str(val)
    return None
