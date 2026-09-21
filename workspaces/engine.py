"""One door to a per-user persistent workspace, whichever engine is configured.

`execute_python` is a dead process per snippet; a pipeline, a test suite or a
scheduled job needs a place that keeps files, keeps packages and keeps
running. That place is a `Workspace`: one per user in v1, idle-hibernated
after 15 min, quotas enforced, destroyed on account deletion.

The same one-door shape as `sandbox/engine.py` and `browsing/engine.py`:

- ``none`` (default) — no workspace. The `compute` tools are not offered at
  all, rather than offered and refusing.
- ``docker`` — local Docker, dev only (gVisor when present).
- ``<provider>`` — the production provider (decision D1): E2B, Daytona, Modal
  sandboxes or Fly Machines, chosen on disk persistence across hibernate,
  idle cost, PTY + port forwarding and India latency.

There is deliberately no automatic fallback between engines.

Isolation rules (non-negotiable): one user per VM/microVM; no platform secrets
inside (no `.env`, no DB credentials); secrets enter only through P0
references into a single command's environment; egress through the P0
allowlist; idle hibernate after 15 min; per-tier quotas metered into
`CostEntry`; destroyed on account deletion.
"""
from __future__ import annotations

import logging

from django.conf import settings

logger = logging.getLogger(__name__)


class WorkspaceError(RuntimeError):
    """Written for the model: what failed and what to do instead."""


def engine() -> str:
    return (getattr(settings, 'WORKSPACE_ENGINE', 'none') or 'none').strip().lower()


def workspace_available() -> bool:
    return engine() not in ('', 'none')


def ensure(user) -> object:
    """The user's workspace, creating and starting it if needed."""
    if not workspace_available():
        raise WorkspaceError('No workspace engine is configured on this platform.')
    from .models import Workspace

    ws, _ = Workspace.objects.get_or_create(
        user=user, defaults={'status': 'creating'},
    )
    return ws


async def exec(ws, cmd: str, *, cwd: str = '', env: dict | None = None,
               timeout: int = 300) -> dict:
    """Run one short command. Returns {exit_code, stdout, stderr}."""
    raise WorkspaceError('No workspace engine is configured on this platform.')


async def read(ws, path: str) -> bytes:
    raise WorkspaceError('No workspace engine is configured on this platform.')


async def write(ws, path: str, data: bytes) -> None:
    raise WorkspaceError('No workspace engine is configured on this platform.')


async def listdir(ws, path: str) -> list:
    raise WorkspaceError('No workspace engine is configured on this platform.')


async def hibernate(ws) -> None:
    raise WorkspaceError('No workspace engine is configured on this platform.')


async def destroy(ws) -> None:
    raise WorkspaceError('No workspace engine is configured on this platform.')
