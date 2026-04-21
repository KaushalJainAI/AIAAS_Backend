"""
Pre-execution validator for MCP nodes in a workflow.

Scans a workflow definition for `mcp_tool` nodes, loads the referenced
servers, and checks that every required credential is present and decryptable
for the given user. Fast-fails a workflow before engine.run_workflow() spins
up, so users get a clear "configure credential X" error instead of a cryptic
runtime failure mid-execution.
"""
from __future__ import annotations

import logging

from .client import get_visible_server_for_user
from .credential_injector import CredentialInjector

logger = logging.getLogger(__name__)


class MCPWorkflowValidationError(Exception):
    """Raised when MCP pre-flight checks fail for a workflow."""

    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__("; ".join(errors) if errors else "MCP workflow validation failed")


def _collect_mcp_server_ids(workflow_json: dict) -> set[int]:
    ids: set[int] = set()
    for node in (workflow_json or {}).get("nodes", []) or []:
        node_type = node.get("type") or node.get("data", {}).get("type")
        if node_type != "mcp_tool":
            continue
        config = node.get("data", {}).get("config") or node.get("config") or {}
        sid = config.get("server_id")
        if sid is None:
            continue
        try:
            ids.add(int(sid))
        except (TypeError, ValueError):
            logger.warning("Workflow MCP node has non-integer server_id: %r", sid)
    return ids


async def validate_mcp_nodes(workflow_json: dict, user) -> list[str]:
    """
    Return a list of human-readable errors. Empty means OK to execute.

    Errors are gathered (not fail-fast) so the user can fix everything in
    one round trip rather than discovering problems one at a time.
    """
    server_ids = _collect_mcp_server_ids(workflow_json)
    if not server_ids:
        return []

    errors: list[str] = []
    for sid in server_ids:
        server = await get_visible_server_for_user(sid, user, enabled_only=False)
        if server is None:
            errors.append(f"Workflow references unavailable MCP server (id={sid}).")
            continue
        if not server.enabled:
            errors.append(f"MCP server '{server.name}' is disabled.")
            continue
        errors.extend(await CredentialInjector.validate(server, user))

    return errors


async def assert_mcp_nodes_valid(workflow_json: dict, user) -> None:
    """Convenience wrapper that raises instead of returning errors."""
    errors = await validate_mcp_nodes(workflow_json, user)
    if errors:
        raise MCPWorkflowValidationError(errors)
