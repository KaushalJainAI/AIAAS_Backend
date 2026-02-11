"""
Compiler Pydantic Schemas

Models for compilation results and execution context.
"""
from uuid import UUID
from typing import Any
from pydantic import BaseModel, Field
import re
import copy


class CompileError(BaseModel):
    """A single compilation error"""
    type: str = Field(default="error", description="Error category: error, warning, info")
    node_id: str | None = Field(default=None, description="Node that caused the error", serialization_alias="nodeId")
    error_type: str = Field(..., description="Error type code", serialization_alias="code")
    message: str = Field(..., description="Human-readable error message")
    field: str | None = Field(default=None, description="Specific field causing error")


class CompileWarning(BaseModel):
    """A compilation warning (non-blocking)"""
    type: str = Field(default="warning", description="Error category: error, warning, info")
    node_id: str | None = Field(default=None, serialization_alias="nodeId")
    warning_type: str = Field(..., serialization_alias="code")
    message: str
    field: str | None = Field(default=None, description="Specific field causing warning")



class ExecutionContext(BaseModel):
    """
    Runtime context passed to each node during execution.
    
    Contains all the state needed for node execution including:
    - Outputs from previously executed nodes
    - Decrypted credentials for integrations
    - Workflow-level variables
    
    Usage:
        context = ExecutionContext(execution_id=uuid, user_id=1, workflow_id=1)
        context.set_node_output("node_1", {"result": "data"})
        data = context.get_node_output("node_1")
    """
    execution_id: UUID = Field(..., description="Unique execution identifier")
    user_id: int = Field(..., description="User running the workflow")
    workflow_id: int = Field(..., description="Workflow being executed")
    workflow_version_id: int | None = Field(default=None, description="Specific version ID if applicable")
    
    # Runtime state
    node_outputs: dict[str, Any] = Field(
        default_factory=dict,
        description="Outputs from previously executed nodes (node_id -> output)"
    )
    credentials: dict[str, Any] = Field(
        default_factory=dict,
        description="Decrypted credentials available to nodes"
    )
    variables: dict[str, Any] = Field(
        default_factory=dict,
        description="Workflow-level variables"
    )
    warnings: list[CompileError] = Field(
        default_factory=list,
        description="Runtime warnings captured during execution"
    )
    
    # Execution tracking
    loop_stats: dict[str, int] = Field(
        default_factory=dict,
        description="Iteration counts for loop nodes (node_id -> count)"
    )
    executed_nodes: list[str] = Field(
        default_factory=list,
        description="List of node IDs that have been executed"
    )
    current_node_id: str | None = Field(
        default=None,
        description="Currently executing node ID"
    )
    
    # Mapping for expression resolution
    node_label_to_id: dict[str, str] = Field(
        default_factory=dict,
        description="Mapping from node labels to their IDs"
    )
    current_input: list[dict] = Field(
        default_factory=list,
        description="Current input items array for the node"
    )
    
    # Configuration
    timeout_seconds: int = Field(default=300, description="Overall execution timeout")
    
    # Subworkflow tracking
    nesting_depth: int = Field(default=0, description="Current nesting depth")
    max_nesting_depth: int = Field(default=3, description="Maximum allowed nesting depth")
    workflow_chain: list[int] = Field(default_factory=list, description="Chain of parent workflow IDs")
    parent_execution_id: UUID | None = Field(default=None, description="ID of parent execution")
    timeout_budget_ms: int | None = Field(default=None, description="Remaining timeout budget in ms")
    
    model_config = {"arbitrary_types_allowed": True}
    
    # ==================== Helper Methods ====================
    def get_node_output(self, node_id: str) -> Any:
        """
        Get the output from a previously executed node.
        
        Args:
            node_id: ID of the node to get output from
            
        Returns:
            The node's output data, or None if not found
        """
        return self.node_outputs.get(node_id)

    def resolve_expressions(self, config: dict, expression_paths: list[list]) -> dict:
        """Resolve all pre-analyzed expressions in the config."""
        if not expression_paths:
            return config
            
        # Create a shallow copy to avoid mutating the original template if it's reused
        # Actually, deepcopy might be safer here since we are modifying nested values
        resolved_config = copy.deepcopy(config)
        
        for path in expression_paths:
            value = self._get_nested_value(resolved_config, path)
            if isinstance(value, str):
                resolved_value = self._resolve_string_expression(value)
                self._set_nested_value(resolved_config, path, resolved_value)
                
        return resolved_config

    def _get_nested_value(self, data: Any, path: list) -> Any:
        for key in path:
            if isinstance(data, dict):
                data = data.get(key)
            elif isinstance(data, list) and isinstance(key, int):
                data = data[key]
            else:
                return None
        return data

    def _set_nested_value(self, data: Any, path: list, value: Any) -> None:
        for i, key in enumerate(path[:-1]):
            if isinstance(data, dict):
                data = data.get(key)
            elif isinstance(data, list) and isinstance(key, int):
                data = data[key]
        
        last_key = path[-1]
        if isinstance(data, dict):
            data[last_key] = value
        elif isinstance(data, list) and isinstance(last_key, int):
            data[last_key] = value

    def _resolve_string_expression(self, text: str) -> Any:
        """Handle {{ $node["Name"].json.field }} style expressions."""
        # Simple case: whole string is an expression
        # e.g. "{{ $node["Name"].json.field }}"
        match = re.fullmatch(r"\{\{\s*(.*?)\s*\}\}", text)
        if match:
            return self._evaluate_expression(match.group(1))
            
        # Complex case: interpolation
        # e.g. "Hello {{ $vars.name }}!"
        def replace_match(m):
            val = self._evaluate_expression(m.group(1))
            return str(val) if val is not None else ""
            
        return re.sub(r"\{\{\s*(.*?)\s*\}\}", replace_match, text)

    def _evaluate_expression(self, expr: str) -> Any:
        """Parse and evaluate a single expression string."""
        expr = expr.strip()
        
        # 1. $node handling - supports $node["Name"], $node['Name'], $node.Name
        # Regex explanation:
        # \$node
        # (?:
        #   \[\s*['\"](.+?)['\"]\s*\]  --> Bracket style: ["Name"] or ['Name']
        #   |
        #   \.([a-zA-Z0-9_\-]+)           --> Dot style: .Name (added dash support)
        # )
        # (?:\.(.*))?                  --> Optional rest of the path: .json.field
        node_match = re.match(r"\$node(?:\[\s*['\"](.+?)['\"]\s*\]|\.([a-zA-Z0-9_\-]+))(?:\.(.*))?", expr)
        if node_match:
            label = node_match.group(1) or node_match.group(2)
            path = node_match.group(3) or ""
            
            # 1.1 Robust node lookup
            node_id = self.node_label_to_id.get(label)
            
            if not node_id:
                # Try ID directly
                if label in self.node_outputs:
                    node_id = label
                else:
                    # Case-insensitive label check as fallback
                    label_lower = label.lower()
                    for l, nid in self.node_label_to_id.items():
                        if l.lower() == label_lower:
                            node_id = nid
                            break
            
            # 1.2 Last ditch: try node type names if no label matches
            if not node_id:
                # This is risky but helpful for single nodes
                possible_matches = []
                # In real execution, we don't have node types here easily,
                # but we can look for node IDs that start with the label or match common patterns
                # Actually, skipping this for now to stay safe, 
                # but we'll improve the error message instead.
                pass

            if not node_id or node_id not in self.node_outputs:
                return None

            output = self.get_node_output(node_id)
            val = self._get_value_by_path(output, path)
            
            if val is None and path:
                self.warnings.append(CompileError(
                    node_id=self.current_node_id,
                    error_type="runtime_expression_missing_field",
                    type="warning",
                    message=f"Path '{path}' not found in node '{label}' output."
                ))
            return val
            
        # 2. $json or $input handling (Current node input)
        elif expr.startswith("$json") or expr.startswith("$input"):
            path = ""
            if expr.startswith("$json."): path = expr[6:]
            elif expr.startswith("$input."): path = expr[7:]
            elif expr.startswith("$json["): path = expr[5:]
            elif expr.startswith("$input["): path = expr[6:]
            
            # n8n $json refers to the current item's json property.
            # In our case, we'll look at self.current_input (which is list of items)
            return self._get_value_by_path(self.current_input, path)
            
        # 3. $vars handling
        elif expr.startswith("$vars."):
            path = expr[6:]
            return self.get_variable(path)

        return None

    def _get_value_by_path(self, obj: Any, path: str) -> Any:
        """Helper to navigate nested structures like 'json.data[0].id' or 'data["score"]'."""
        if obj is None: return None
        if not path: return obj
        
        import re
        # Tokens can be words (dot style), digits (array index), or quoted strings (bracket style)
        # matches: word | [digit] | ["string"] | ['string']
        pattern = r'(\w+)|\[\s*(?:(\d+)|[\'"](.+?)[\'"])\s*\]'
        
        current = obj
        for match in re.finditer(pattern, path):
            word, index, bracket_key = match.groups()
            token = word or index or bracket_key
            
            if current is None: return None
            
            # If we are at a list, and trying to access a field, it might be an items list
            # n8n automatically handles $node["name"].json.field even if .json is a list field?
            # Actually, if current is a list of items, we should look into the first item's json if the token isn't an index.
            if isinstance(current, list) and index is None:
                if len(current) > 0:
                    first = current[0]
                    if isinstance(first, dict) and "json" in first:
                        # If we are looking for 'json' specifically, and it's already an item
                        if token == "json":
                            current = first["json"]
                            continue
                        else:
                            # Not looking for 'json', but we are on a list. 
                            # Try to see if we should auto-dive into first item
                            current = first["json"].get(token) if isinstance(first["json"], dict) else None
                            continue
                    else:
                        # Not an items list, or empty
                        return None
                else:
                    return None

            if index:
                idx = int(index)
                if isinstance(current, list) and 0 <= idx < len(current):
                    current = current[idx]
                else:
                    return None
            else:
                # word or bracket_key
                if isinstance(current, dict):
                    current = current.get(token)
                else:
                    return None
                    
        return current
    
    def set_node_output(self, node_id: str, output: Any) -> None:
        """
        Store the output from an executed node.
        
        Args:
            node_id: ID of the node that produced the output
            output: The output data to store
        """
        self.node_outputs[node_id] = output
        if node_id not in self.executed_nodes:
            self.executed_nodes.append(node_id)
    
    def get_input_for_node(self, node_id: str, edges: list[dict]) -> list[dict]:
        """
        Collect input items for a node from its upstream connections.
        
        Returns n8n-style items array where each item has a 'json' key.
        
        Args:
            node_id: ID of the target node
            edges: List of edge definitions with 'source' and 'target'
            
        Returns:
            List of items in format [{"json": {...}}, {"json": {...}}]
        """
        items = []
        
        # Find all edges targeting this node
        for edge in edges:
            if edge.get("target") == node_id:
                source_id = edge.get("source")
                source_output = self.get_node_output(source_id)
                
                if source_output is None:
                    continue
                    
                # Handle different output formats
                if isinstance(source_output, list):
                    # Already in items format
                    for item in source_output:
                        if isinstance(item, dict):
                            if "json" in item:
                                items.append(item)
                            else:
                                items.append({"json": item})
                elif isinstance(source_output, dict):
                    if "json" in source_output:
                        items.append(source_output)
                    elif "items" in source_output:
                        # NodeExecutionResult-like structure
                        for item in source_output.get("items", []):
                            items.append(item if "json" in item else {"json": item})
                    else:
                        items.append({"json": source_output})
                else:
                    items.append({"json": {"value": source_output}})
        
        return items
    
    def get_credential(self, credential_id: str) -> Any:
        """
        Get a decrypted credential by ID.
        
        Args:
            credential_id: ID or name of the credential
            
        Returns:
            Decrypted credential data, or None if not found
        """
        return self.credentials.get(credential_id)
    
    def set_variable(self, name: str, value: Any) -> None:
        """Set a workflow-level variable."""
        self.variables[name] = value
    
    def get_variable(self, name: str, default: Any = None) -> Any:
        """Get a workflow-level variable."""
        return self.variables.get(name, default)
    
    def has_executed(self, node_id: str) -> bool:
        """Check if a node has already been executed."""
        return node_id in self.executed_nodes
    
    # ==================== Loop State Management ====================
    def get_loop_count(self, node_id: str) -> int:
        """Get current iteration count for a loop node."""
        return self.loop_stats.get(node_id, 0)
    
    def increment_loop(self, node_id: str) -> int:
        """Increment and return the loop count for a node."""
        current = self.loop_stats.get(node_id, 0)
        self.loop_stats[node_id] = current + 1
        return current + 1
    
    def get_batch_cursor(self, node_id: str) -> int:
        """Get current batch cursor position for a loop node."""
        return self.variables.get(f"_cursor_{node_id}", 0)
    
    def set_batch_cursor(self, node_id: str, cursor: int) -> None:
        """Update batch cursor position for a loop node."""
        self.variables[f"_cursor_{node_id}"] = cursor
    
    def get_loop_items(self, node_id: str) -> list:
        """Get the items being iterated over by a loop node."""
        return self.variables.get(f"_items_{node_id}", [])
    
    def set_loop_items(self, node_id: str, items: list) -> None:
        """Store items to iterate over for a loop node."""
        self.variables[f"_items_{node_id}"] = items
    
    def accumulate_loop_result(self, node_id: str, result: Any) -> None:
        """Add a result to the accumulated loop outputs."""
        key = f"_accumulated_{node_id}"
        if key not in self.variables:
            self.variables[key] = []
        self.variables[key].append(result)
    
    def get_accumulated_results(self, node_id: str) -> list:
        """Get all accumulated results from loop iterations."""
        return self.variables.get(f"_accumulated_{node_id}", [])



class NodeExecutionPlan(BaseModel):
    """
    Execution configuration for a single node.
    (Kept for internal use if needed, but mostly deprecated by dict lookups)
    """
    node_id: str
    node_type: str
    config: dict[str, Any]
    dependencies: list[str] = Field(default_factory=list)
    timeout_seconds: int = 60
