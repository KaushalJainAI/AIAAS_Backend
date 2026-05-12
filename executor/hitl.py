"""
Human-in-the-loop request types.

The full HITL queue lives on the KingOrchestrator instance (so cancellation
and auth can reuse its locks); only the value types live here.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any
from uuid import UUID

from workflow_backend.thresholds import DEFAULT_HITL_TIMEOUT_SECONDS


class HITLRequestType(str, Enum):
    APPROVAL = "approval"
    CLARIFICATION = "clarification"
    ERROR_RECOVERY = "error_recovery"
    REVIEW = "review"


@dataclass
class HITLRequest:
    """A single human-in-the-loop interaction request."""
    id: str
    request_type: HITLRequestType
    execution_id: UUID
    user_id: int
    node_id: str
    message: str
    options: list[str] = field(default_factory=list)
    timeout_seconds: int = DEFAULT_HITL_TIMEOUT_SECONDS
    created_at: datetime = field(default_factory=datetime.utcnow)
    response: Any = None
    responded_at: datetime | None = None
