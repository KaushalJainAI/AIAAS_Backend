"""
Compiler Utilities
"""
from typing import Any

def get_node_type(node: dict[str, Any]) -> str:
    """
    Extract node type from node definition, prioritizing frontend conventions.
    
    Priority:
    1. node['nodeType'] (Direct camelCase)
    2. node['data']['nodeType'] (Frontend data payload)
    3. node['type'] (ReactFlow generic type, fallback)
    """
    return (
        node.get('nodeType') or 
        node.get('data', {}).get('nodeType') or 
        node.get('type', '')
    )
