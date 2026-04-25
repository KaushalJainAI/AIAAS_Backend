"""
Test-data generator + validator for workflows.

Produces a synthetic input payload shaped like what each kind of trigger
typically emits, so a workflow can be exercised end-to-end in tests without
wiring real triggers.
"""
from __future__ import annotations

import logging
import random
from dataclasses import dataclass, field
from typing import Any, Dict

from compiler.config_access import get_node_config
from compiler.utils import get_node_type
from orchestrator.models import Workflow

logger = logging.getLogger(__name__)


@dataclass
class ValidationResult:
    passed: bool
    error: str = ""
    details: Dict[str, Any] = field(default_factory=dict)


# Per-trigger-type sample payload generators. Each returns a dict that will
# be merged into the workflow's initial input.
def _manual(_config: dict) -> dict:
    return {
        "text": "Sample test text",
        "number": random.randint(1, 100),
        "boolean": True,
    }


def _webhook(_config: dict) -> dict:
    return {
        "body": {"message": "Test webhook payload", "id": 123},
        "headers": {"Content-Type": "application/json"},
        "query": {"test": "true"},
    }


def _email(_config: dict) -> dict:
    return {
        "subject": "Test Email Subject",
        "from": "test@example.com",
        "body": "This is a test email body.",
    }


_TRIGGER_GENERATORS = {
    "manual_trigger": _manual,
    "webhook_trigger": _webhook,
    "email_trigger": _email,
}


def generate_test_input(workflow: Workflow) -> Dict[str, Any]:
    """Build a synthetic input payload based on the workflow's triggers."""
    input_data: Dict[str, Any] = {}
    for node in workflow.nodes or []:
        ntype = get_node_type(node)
        gen = _TRIGGER_GENERATORS.get(ntype)
        if gen is None and ntype and ntype.endswith("trigger"):
            # Unknown trigger type — emit an empty payload rather than silently skip.
            gen = lambda _cfg: {}
        if gen:
            input_data.update(gen(get_node_config(node)))
    return input_data


def validate_test_result(
    result: Dict[str, Any],
    expected_schema: Dict[str, Any] | None = None,
    timeout_limit_ms: int = 30000,
) -> ValidationResult:
    """
    Validate an execution's final output against an optional schema.

    Only presence of declared keys is checked today; type checks are an
    explicit extension point.
    """
    if expected_schema:
        for key in expected_schema:
            if key not in result:
                return ValidationResult(False, f"Missing expected output key: {key}")
    return ValidationResult(True, details={"matches_schema": True})
