"""
Workflow Validators

DAG validation, credential checking, and type compatibility.
"""
from typing import Any
from collections import defaultdict

from .schemas import CompileError


def validate_dag(nodes: list[dict], edges: list[dict]) -> list[CompileError]:
    """
    Validate the workflow is a valid DAG.
    
    Checks:
    - No cycles exist
    - No orphan nodes (unreachable from triggers)
    - All edge references are valid
    
    Args:
        nodes: List of node definitions with 'id' and 'type'
        edges: List of edge definitions with 'source' and 'target'
    
    Returns:
        List of CompileError if validation fails
    """
    errors = []
    
    if not nodes:
        errors.append(CompileError(
            error_type="empty_workflow",
            message="Workflow has no nodes"
        ))
        return errors
    
    # Build node lookup and adjacency list
    node_ids = {node['id'] for node in nodes}
    node_types = {node['id']: node.get('type', '') for node in nodes}
    
    # Adjacency list: node_id -> list of downstream node_ids
    adjacency: dict[str, list[str]] = defaultdict(list)
    in_degree: dict[str, int] = {nid: 0 for nid in node_ids}
    
    # Validate edges and build graph
    for edge in edges:
        source = edge.get('source')
        target = edge.get('target')
        
        if source not in node_ids:
            errors.append(CompileError(
                node_id=source,
                error_type="invalid_edge",
                message=f"Edge source '{source}' does not exist"
            ))
            continue
        
        if target not in node_ids:
            errors.append(CompileError(
                node_id=target,
                error_type="invalid_edge",
                message=f"Edge target '{target}' does not exist"
            ))
            continue
        
        adjacency[source].append(target)
        in_degree[target] += 1
    
    if errors:
        return errors
    
    # Detect cycles using DFS
    visited = set()
    rec_stack = set()
    cycle_node = None
    
    def has_cycle(node_id: str) -> bool:
        nonlocal cycle_node
        visited.add(node_id)
        rec_stack.add(node_id)
        
        for neighbor in adjacency[node_id]:
            if neighbor not in visited:
                if has_cycle(neighbor):
                    return True
            elif neighbor in rec_stack:
                cycle_node = neighbor
                return True
        
        rec_stack.remove(node_id)
        return False
    
    for node_id in node_ids:
        if node_id not in visited:
            if has_cycle(node_id):
                errors.append(CompileError(
                    node_id=cycle_node,
                    error_type="dag_cycle",
                    message=f"Cycle detected involving node '{cycle_node}'"
                ))
                return errors  # Stop at first cycle
    
    # Find trigger nodes (no incoming edges)
    trigger_types = {'manual_trigger', 'webhook_trigger', 'schedule_trigger'}
    triggers = [nid for nid in node_ids if in_degree[nid] == 0]
    
    if not triggers:
        errors.append(CompileError(
            error_type="no_trigger",
            message="Workflow has no trigger node (entry point)"
        ))
    
    # Check for orphan nodes (not reachable from any trigger)
    reachable = set()
    
    def mark_reachable(node_id: str):
        if node_id in reachable:
            return
        reachable.add(node_id)
        for neighbor in adjacency[node_id]:
            mark_reachable(neighbor)
    
    for trigger in triggers:
        mark_reachable(trigger)
    
    orphans = node_ids - reachable
    for orphan in orphans:
        errors.append(CompileError(
            node_id=orphan,
            error_type="orphan_node",
            message=f"Node '{orphan}' is not reachable from any trigger"
        ))
    
    return errors


def validate_credentials(
    nodes: list[dict],
    user_credentials: set[str]
) -> list[CompileError]:
    """
    Validate user has required credentials for all nodes.
    
    Args:
        nodes: List of node definitions
        user_credentials: Set of credential IDs the user has
    
    Returns:
        List of CompileError for missing credentials
    """
    errors = []
    
    for node in nodes:
        node_id = node.get('id', '')
        config = node.get('data', {}).get('config', {})
        
        # Check if node uses a credential field
        credential_id = config.get('credential')
        if credential_id and credential_id not in user_credentials:
            errors.append(CompileError(
                node_id=node_id,
                error_type="missing_credential",
                message=f"Credential '{credential_id}' not found for node"
            ))
    
    return errors


def validate_node_configs(nodes: list[dict]) -> list[CompileError]:
    """
    Validate node configurations are complete.
    
    Args:
        nodes: List of node definitions
    
    Returns:
        List of CompileError for invalid configs
    """
    from nodes.handlers.registry import get_registry
    
    errors = []
    registry = get_registry()
    
    for node in nodes:
        node_id = node.get('id', '')
        node_type = node.get('type', '')
        config = node.get('data', {}).get('config', {})
        
        if not registry.has_handler(node_type):
            errors.append(CompileError(
                node_id=node_id,
                error_type="unknown_node_type",
                message=f"Unknown node type: '{node_type}'"
            ))
            continue
        
        # Validate config against handler's fields
        handler = registry.get_handler(node_type)
        config_errors = handler.validate_config(config)
        
        for error_msg in config_errors:
            errors.append(CompileError(
                node_id=node_id,
                error_type="invalid_config",
                message=error_msg
            ))
    
    return errors


def topological_sort(nodes: list[dict], edges: list[dict]) -> list[str]:
    """
    Return nodes in topological order (dependencies first).
    
    Uses Kahn's algorithm.
    """
    node_ids = {node['id'] for node in nodes}
    in_degree: dict[str, int] = {nid: 0 for nid in node_ids}
    adjacency: dict[str, list[str]] = defaultdict(list)
    
    for edge in edges:
        source = edge.get('source')
        target = edge.get('target')
        if source in node_ids and target in node_ids:
            adjacency[source].append(target)
            in_degree[target] += 1
    
    # Start with nodes that have no dependencies
    queue = [nid for nid in node_ids if in_degree[nid] == 0]
    result = []
    
    while queue:
        node_id = queue.pop(0)
        result.append(node_id)
        
        for neighbor in adjacency[node_id]:
            in_degree[neighbor] -= 1
            if in_degree[neighbor] == 0:
                queue.append(neighbor)
    
    return result


# Output type hints for nodes (what type of data they produce)
NODE_OUTPUT_TYPES = {
    # Triggers produce generic data
    'manual_trigger': {'main': 'any'},
    'webhook_trigger': {'main': 'json'},
    'schedule_trigger': {'main': 'datetime'},
    
    # Core nodes
    'http_request': {'success': 'json', 'error': 'error'},
    'code': {'success': 'any', 'error': 'error'},
    'set': {'output': 'json'},
    'if': {'true': 'passthrough', 'false': 'passthrough'},
    
    # LLM nodes produce text
    'openai': {'success': 'text', 'error': 'error'},
    'gemini': {'success': 'text', 'error': 'error'},
    'ollama': {'success': 'text', 'error': 'error'},
    
    # Integration nodes
    'gmail': {'success': 'json', 'error': 'error'},
    'slack': {'success': 'json', 'error': 'error'},
    'google_sheets': {'success': 'json', 'error': 'error'},
}

# Input type expectations (what types a node can accept)
NODE_INPUT_TYPES = {
    'http_request': ['json', 'any', 'text', 'passthrough'],
    'code': ['json', 'any', 'text', 'passthrough'],
    'set': ['json', 'any', 'text', 'passthrough'],
    'if': ['json', 'any', 'text', 'passthrough'],
    'openai': ['json', 'any', 'text', 'passthrough'],
    'gemini': ['json', 'any', 'text', 'passthrough'],
    'ollama': ['json', 'any', 'text', 'passthrough'],
    'gmail': ['json', 'any', 'text', 'passthrough'],
    'slack': ['json', 'any', 'text', 'passthrough'],
    'google_sheets': ['json', 'any', 'passthrough'],
}


def validate_type_compatibility(
    nodes: list[dict],
    edges: list[dict]
) -> list[CompileError]:
    """
    Validate type compatibility between connected nodes.
    
    Checks that the output type of a source node is compatible with
    the expected input type of the target node.
    
    Args:
        nodes: List of node definitions
        edges: List of edge definitions
        
    Returns:
        List of CompileError for type mismatches
    """
    errors = []
    node_types = {node['id']: node.get('type', '') for node in nodes}
    
    for edge in edges:
        source_id = edge.get('source')
        target_id = edge.get('target')
        source_handle = edge.get('sourceHandle', 'output')
        
        source_type = node_types.get(source_id, '')
        target_type = node_types.get(target_id, '')
        
        # Get output type from source node
        source_outputs = NODE_OUTPUT_TYPES.get(source_type, {'output': 'any'})
        output_type = source_outputs.get(source_handle, 'any')
        
        # Get acceptable input types for target node
        acceptable_inputs = NODE_INPUT_TYPES.get(target_type, ['any'])
        
        # Check compatibility
        if output_type == 'error':
            # Error outputs can only connect to error handlers or nodes that accept errors
            if 'error' not in acceptable_inputs and 'any' not in acceptable_inputs:
                errors.append(CompileError(
                    node_id=target_id,
                    error_type='type_mismatch',
                    message=f"Node '{target_id}' cannot accept error output from '{source_id}'"
                ))
        elif output_type not in ['any', 'passthrough']:
            # Check if output type is in acceptable inputs
            if output_type not in acceptable_inputs and 'any' not in acceptable_inputs:
                errors.append(CompileError(
                    node_id=target_id,
                    error_type='type_mismatch',
                    message=f"Type mismatch: '{source_type}' outputs '{output_type}' but '{target_type}' expects {acceptable_inputs}"
                ))
    
    return errors
