"""
`ExecutionContext` — what a provider handler is handed besides its config.

Every handler call takes one. It carries the two things a handler cannot get
from its config: whose credentials to spend, and somewhere to memoize the ones
already fetched.

It used to be the DAG runtime's state bag, ~460 lines of per-node output
storage, `{{ $node["x"].y }}` template resolution, loop counters, batch
cursors, subworkflow nesting and warning accumulation — all reachable only from
the graph executor, which went with the workflow product. What survived that
cut was still carrying four fields nothing used:

    execution_id   write-only; both call sites minted a throwaway uuid4()
    workflow_id    0 at every call site, and justified in its own docstring by
                   the fact that call sites passed it
    variables      + set_variable() / get_variable(), whose only callers were
                   their own unit tests
    skills         always [], so the "merge workflow-level skills" half of
                   `resolve_node_skills` could never merge anything

This lived in a `compiler` app that held nothing else — no models, no
migrations, no routes — while four of its five importers were in `llm/`. It is
here now, and that app is gone.
"""
from __future__ import annotations

import logging
from typing import Any

from pydantic import BaseModel, Field, field_validator

logger = logging.getLogger(__name__)


class ExecutionContext(BaseModel):
    """Runtime context passed to each provider handler during execution."""

    user_id: int

    #: Credentials already resolved for this context, keyed by id. Populated by
    #: `get_credential` so repeated lookups in one handler call hit memory.
    credentials: dict[str, Any] = Field(default_factory=dict)

    model_config = {"arbitrary_types_allowed": True}

    @field_validator("credentials", mode="before")
    @classmethod
    def _none_to_empty_dict(cls, v: Any) -> dict:
        return v if v is not None else {}

    async def get_credential(self, credential_id: str | int | None) -> Any:
        """Fetch a credential, preferring one already resolved in this context."""
        if not credential_id:
            return None
        sid = str(credential_id)
        if sid in self.credentials:
            return self.credentials[sid]

        try:
            from credentials.manager import get_credential_manager
            manager = get_credential_manager()
            data = await manager.get_credential(credential_id, user_id=self.user_id)
            if data:
                self.credentials[sid] = data
                return data
        except Exception as e:
            # Missing creds are a common, recoverable user error: the caller
            # turns None into a "no verified credential" message. This used to
            # `from logs.logger import logger` here, which meant the error path
            # raised ModuleNotFoundError once that module was deleted — the one
            # branch that must not fail was the one that did.
            logger.error(f"Failed to fetch credential {credential_id}: {e}")
        return None
