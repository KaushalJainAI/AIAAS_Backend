"""
Workflow validators.

Each function takes the raw ReactFlow-style node/edge lists and returns a list
of CompileError objects (possibly empty). The compiler aggregates these and
decides which are blocking vs. warnings.

Public API (consumed by orchestrator/views.py):
    - validate_dag
    - validate_credentials
    - validate_node_configs
    - validate_type_compatibility
    - topological_sort (consumed by compiler.py)
"""
from __future__ import annotations

import re
from collections import defaultdict, deque
from typing import Any

from .config_access import get_credential_ref, get_node_config, get_node_data
from .node_types import LOOP_NODE_TYPES, TRIGGER_NODE_TYPES
from .schemas import CompileError
from .utils import get_node_type


def validate_dag(nodes: list[dict], edges: list[dict]) -> list[CompileError]:
    """
    Validate the workflow is a valid DAG, with one allowed exception: cycles
    that close back on a loop-type node are permitted (they model iteration).

    Checks:
        - Non-empty node set
        - All edge endpoints reference known nodes
        - No cycles except those whose back-edge target is a loop node
        - At least one trigger (zero in-degree) node exists
        - All nodes are reachable from some trigger
    """
    errors: list[CompileError] = []

    if not nodes:
        errors.append(CompileError(
            error_type="empty_workflow",
            message="Workflow has no nodes",
        ))
        return errors

    node_ids = [n["id"] for n in nodes]
    node_id_set = set(node_ids)
    node_types = {n["id"]: get_node_type(n) for n in nodes}

    adjacency: dict[str, list[str]] = defaultdict(list)
    in_degree: dict[str, int] = dict.fromkeys(node_ids, 0)

    # 1. Validate edge endpoints and build adjacency.
    edge_errors_exist = False
    for edge in edges:
        src, tgt = edge.get("source"), edge.get("target")
        if src not in node_id_set:
            errors.append(CompileError(
                node_id=src, error_type="invalid_edge",
                message=f"Edge source '{src}' does not exist",
            ))
            edge_errors_exist = True
            continue
        if tgt not in node_id_set:
            errors.append(CompileError(
                node_id=tgt, error_type="invalid_edge",
                message=f"Edge target '{tgt}' does not exist",
            ))
            edge_errors_exist = True
            continue
        adjacency[src].append(tgt)
        in_degree[tgt] += 1

    # Structural edge issues invalidate downstream cycle/orphan checks.
    if edge_errors_exist:
        return errors

    # 2. Detect illegal cycles (cycles not closed on a loop node).
    errors.extend(_find_illegal_cycles(node_ids, adjacency, node_types))
    if errors:
        return errors

    # 3. Trigger presence — at least one zero-in-degree node.
    triggers = [nid for nid in node_ids if in_degree[nid] == 0]
    if not triggers:
        errors.append(CompileError(
            error_type="no_trigger",
            message="Workflow has no trigger node (entry point)",
        ))

    # 4. Orphan check — all nodes must be reachable from some zero-in-degree node.
    reachable: set[str] = set()
    stack = list(triggers)
    while stack:
        nid = stack.pop()
        if nid in reachable:
            continue
        reachable.add(nid)
        stack.extend(adjacency[nid])

    for orphan in sorted(set(node_ids) - reachable):
        errors.append(CompileError(
            node_id=orphan, error_type="orphan_node",
            message=f"Node '{orphan}' is not reachable from any trigger",
        ))

    return errors


def _find_illegal_cycles(
    node_ids: list[str],
    adjacency: dict[str, list[str]],
    node_types: dict[str, str],
) -> list[CompileError]:
    """
    DFS-based cycle detection. A cycle is legal iff the back-edge target
    is a loop-type node (the loop node is the cycle's header).
    """
    errors: list[CompileError] = []
    visited: set[str] = set()
    on_stack: set[str] = set()
    path: list[str] = []

    def dfs(start: str) -> bool:
        # Iterative DFS to avoid Python recursion limits on deep graphs.
        # We push (node, iterator-over-neighbors) frames.
        frame_stack: list[tuple[str, iter]] = [(start, iter(sorted(adjacency[start])))]
        visited.add(start)
        on_stack.add(start)
        path.append(start)

        while frame_stack:
            node, it = frame_stack[-1]
            neighbor = next(it, None)

            if neighbor is None:
                on_stack.discard(node)
                path.pop()
                frame_stack.pop()
                continue

            if neighbor in on_stack:
                # Back-edge. Legal only if neighbor is a loop-header.
                if node_types.get(neighbor) not in LOOP_NODE_TYPES:
                    try:
                        cycle_path = path[path.index(neighbor):]
                    except ValueError:
                        cycle_path = [neighbor]
                    errors.append(CompileError(
                        node_id=neighbor, error_type="dag_cycle",
                        message=f"Infinite cycle detected involving nodes: {', '.join(cycle_path)}",
                    ))
                    return True
                continue

            if neighbor not in visited:
                visited.add(neighbor)
                on_stack.add(neighbor)
                path.append(neighbor)
                frame_stack.append((neighbor, iter(sorted(adjacency[neighbor]))))

        return False

    for nid in node_ids:
        if nid not in visited:
            if dfs(nid):
                return errors

    return errors


def validate_credentials(
    nodes: list[dict], user_credentials: set[str],
) -> list[CompileError]:
    """
    For each node that references a credential, verify the user owns it.

    Accepts credential references under any of: credential_id, credentialId,
    credential — see config_access.get_credential_ref.
    """
    errors: list[CompileError] = []
    for node in nodes:
        cfg = get_node_config(node)
        cred_id = get_credential_ref(cfg)
        if cred_id and cred_id not in user_credentials:
            errors.append(CompileError(
                node_id=node.get("id", ""),
                error_type="missing_credential",
                message=f"Credential '{cred_id}' not found for node",
            ))
    return errors


# Loop iteration ceilings. Below _MIN_LOOP treated as misconfiguration; above
# _MAX_LOOP treated as a footgun (runaway bills / executions).
_MIN_LOOP_COUNT = 1
_MAX_LOOP_COUNT = 1000


def validate_node_configs(nodes: list[dict]) -> list[CompileError]:
    """
    Validate node configs against handler requirements and type-specific rules.
    """
    from nodes.handlers.registry import get_registry

    errors: list[CompileError] = []
    registry = get_registry()

    for node in nodes:
        node_id = node.get("id", "")
        node_type = get_node_type(node)
        cfg = get_node_config(node)

        if not registry.has_handler(node_type):
            errors.append(CompileError(
                node_id=node_id, error_type="unknown_node_type",
                message=f"Unknown node type: '{node_type}'",
            ))
            continue

        if node_type in LOOP_NODE_TYPES:
            errors.extend(_validate_loop_config(node_id, cfg))

        handler = registry.get_handler(node_type)
        for msg in handler.validate_config(cfg):
            errors.append(CompileError(
                node_id=node_id, error_type="invalid_config", message=msg,
            ))

        errors.extend(_validate_expressions(node))

    return errors


def _validate_loop_config(node_id: str, cfg: dict) -> list[CompileError]:
    out: list[CompileError] = []
    max_loop = cfg.get("max_loop_count")
    if max_loop is None:
        out.append(CompileError(
            node_id=node_id, error_type="missing_config",
            message="Loop nodes must have 'max_loop_count' defined",
        ))
    elif not isinstance(max_loop, int) or max_loop < _MIN_LOOP_COUNT:
        out.append(CompileError(
            node_id=node_id, error_type="invalid_config",
            message="'max_loop_count' must be a positive integer",
        ))
    elif max_loop > _MAX_LOOP_COUNT:
        out.append(CompileError(
            node_id=node_id, error_type="invalid_config",
            message=f"'max_loop_count' cannot exceed {_MAX_LOOP_COUNT}",
        ))
    return out


# Matches {{ $node['label'].path }} / {{ $node["label"].path }} usage.
_NODE_EXPR_RE = re.compile(r"\{\{\s*\$node\[['\"]([^'\"]+)['\"]\]\.(.*?)\s*\}\}")


def _validate_expressions(node: dict) -> list[CompileError]:
    """
    Scan node config for expression syntax. Currently a no-op: historically
    this emitted warnings for references to non-existent nodes, but runtime
    semantics (variables, event payloads) make that unreliable — the check
    produced false positives and was intentionally suppressed upstream.

    Kept as an extension point; reinstating proper expression validation is
    a larger task (would require a proper expression parser).
    """
    # The regex is retained so future checks can re-enable scanning without
    # reworking the traversal. Touch it to keep the import meaningful.
    _ = _NODE_EXPR_RE
    return []


def topological_sort(nodes: list[dict], edges: list[dict]) -> list[str]:
    """
    Return nodes in a stable, deterministic execution order.

    Rules:
        1. Among zero-in-degree candidates, prefer input order.
        2. When a node is dequeued, newly-zero-in-degree nodes are enqueued
           in input order.
        3. If cycles remain (allowed loop-back edges), unprocessed nodes are
           appended at the end in input order.
    """
    node_ids = [n["id"] for n in nodes]
    idx = {nid: i for i, nid in enumerate(node_ids)}
    node_id_set = set(node_ids)

    adjacency: dict[str, list[str]] = defaultdict(list)
    in_degree: dict[str, int] = dict.fromkeys(node_ids, 0)

    for edge in edges:
        s, t = edge.get("source"), edge.get("target")
        if s in node_id_set and t in node_id_set:
            adjacency[s].append(t)
            in_degree[t] += 1

    for nid in adjacency:
        adjacency[nid].sort(key=lambda x: idx[x])

    ready = deque(sorted([n for n in node_ids if in_degree[n] == 0], key=idx.get))
    result: list[str] = []

    while ready:
        # Maintain input-order priority across newly-added items.
        if len(ready) > 1:
            items = sorted(ready, key=idx.get)
            ready = deque(items)
        node_id = ready.popleft()
        result.append(node_id)

        for nbr in adjacency[node_id]:
            in_degree[nbr] -= 1
            if in_degree[nbr] == 0:
                ready.append(nbr)

    if len(result) < len(node_ids):
        remaining = [nid for nid in node_ids if nid not in set(result)]
        remaining.sort(key=idx.get)
        result.extend(remaining)

    return result


# ---------------------------------------------------------------------------
# Type-compatibility validation.
#
# This is a best-effort static check. The table below covers common node types;
# unknown types fall through as "accept any", so the check never false-fails
# on a new handler.
# ---------------------------------------------------------------------------

NODE_OUTPUT_TYPES: dict[str, dict[str, str]] = {
    # Triggers
    "manual_trigger": {"main": "any"},
    "webhook_trigger": {"main": "json"},
    "schedule_trigger": {"main": "datetime"},
    "webhook": {"main": "json"},
    # Core
    "http_request": {"output-0": "json"},
    "code": {"output-0": "any"},
    "set": {"output": "json"},
    "if": {"true": "passthrough", "false": "passthrough"},
    "switch": {f"output-{i}": "passthrough" for i in range(4)},
    "merge": {"output": "any"},
    # Flow control
    "split_in_batches": {"loop": "any", "done": "any"},
    "loop": {"loop": "any", "done": "any"},
    # LLMs
    "openai": {"output-0": "text"},
    "gemini": {"output-0": "text"},
    "ollama": {"output-0": "text"},
    "perplexity": {"output-0": "text"},
    "openrouter": {"output-0": "text"},
    # Integrations
    "gmail": {"output-0": "json"},
    "slack": {"output-0": "json"},
    "google_sheets": {"output-0": "json"},
    "notion": {"output-0": "json"},
    "postgres": {"output-0": "json"},
    "mysql": {"output-0": "json"},
    "mongodb": {"output-0": "json"},
    "redis": {"output-0": "json"},
    "airtable": {"output-0": "json"},
    "telegram": {"output-0": "json"},
    "trello": {"output-0": "json"},
    "github": {"output-0": "json"},
    "discord": {"output-0": "json"},
    "subworkflow": {"output-0": "any"},
}

_ANY_INPUT = ["json", "any", "text", "passthrough"]
NODE_INPUT_TYPES: dict[str, list[str]] = {
    "http_request": _ANY_INPUT,
    "code": _ANY_INPUT,
    "set": _ANY_INPUT,
    "if": _ANY_INPUT,
    "switch": _ANY_INPUT,
    "merge": _ANY_INPUT,
    "split_in_batches": ["json", "any", "list", "passthrough"],
    "loop": ["json", "any", "list", "passthrough"],
    "openai": _ANY_INPUT,
    "gemini": _ANY_INPUT,
    "ollama": _ANY_INPUT,
    "gmail": _ANY_INPUT,
    "slack": _ANY_INPUT,
    "google_sheets": ["json", "any", "passthrough"],
    "subworkflow": ["json", "any", "passthrough"],
}


def validate_type_compatibility(
    nodes: list[dict], edges: list[dict],
) -> list[CompileError]:
    """
    Static type-compat check on each edge. Unknown node types pass.
    """
    errors: list[CompileError] = []
    node_types = {n["id"]: get_node_type(n) for n in nodes}

    for edge in edges:
        src_id = edge.get("source")
        tgt_id = edge.get("target")
        src_handle = edge.get("sourceHandle", "output")

        src_type = node_types.get(src_id, "")
        tgt_type = node_types.get(tgt_id, "")

        src_outputs = NODE_OUTPUT_TYPES.get(src_type, {"output": "any"})
        out_type = src_outputs.get(src_handle, "any")

        accepted = NODE_INPUT_TYPES.get(tgt_type, ["any"])

        if out_type == "error":
            if "error" not in accepted and "any" not in accepted:
                errors.append(CompileError(
                    node_id=tgt_id, error_type="type_mismatch",
                    message=f"Node '{tgt_id}' cannot accept error output from '{src_id}'",
                ))
        elif out_type not in ("any", "passthrough"):
            if out_type not in accepted and "any" not in accepted:
                errors.append(CompileError(
                    node_id=tgt_id, error_type="type_mismatch",
                    message=(
                        f"Type mismatch: '{src_type}' outputs '{out_type}' "
                        f"but '{tgt_type}' expects {accepted}"
                    ),
                ))

    return errors
