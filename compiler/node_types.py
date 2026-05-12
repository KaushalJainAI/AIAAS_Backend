"""
Single source of truth for node-type categories used by compiler/validators.

Keep this list stable — other apps may import these sets.
"""
from __future__ import annotations

# Nodes that can emit multiple output handles and need conditional routing.
CONDITIONAL_NODE_TYPES: frozenset[str] = frozenset({
    "if",
    "if_condition",
    "switch",
    "loop",
    "split_in_batches",
})

# Nodes that iterate and feed their body's output back into themselves.
LOOP_NODE_TYPES: frozenset[str] = frozenset({
    "loop",
    "split_in_batches",
})

# Workflow entry-point node types (no incoming edges in a well-formed graph).
TRIGGER_NODE_TYPES: frozenset[str] = frozenset({
    "manual_trigger",
    "webhook_trigger",
    "schedule_trigger",
    "webhook",
})
