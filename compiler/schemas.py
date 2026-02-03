"""
Compiler Pydantic Schemas

Models for compilation results and execution context.
"""
from uuid import UUID
from typing import Any
from pydantic import BaseModel, Field


class CompileError(BaseModel):
    """A single compilation error"""
    node_id: str | None = Field(default=None, description="Node that caused the error")
    error_type: str = Field(..., description="Error type: dag_cycle, missing_credential, type_mismatch, invalid_config")
    message: str = Field(..., description="Human-readable error message")


class CompileWarning(BaseModel):
    """A compilation warning (non-blocking)"""
    node_id: str | None = None
    warning_type: str
    message: str



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
    
    def get_input_for_node(self, node_id: str, edges: list[dict]) -> dict[str, Any]:
        """
        Collect input data for a node from its upstream connections.
        
        Args:
            node_id: ID of the target node
            edges: List of edge definitions with 'source' and 'target'
            
        Returns:
            Merged dict of all upstream node outputs
        """
        input_data = {}
        
        # Find all edges targeting this node
        for edge in edges:
            if edge.get("target") == node_id:
                source_id = edge.get("source")
                source_output = self.get_node_output(source_id)
                if source_output:
                    # Merge upstream output into input
                    if isinstance(source_output, dict):
                        input_data.update(source_output)
                    else:
                        input_data[source_id] = source_output
        
        return input_data
    
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
