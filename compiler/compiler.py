"""
Workflow Compiler

Validates and compiles workflow definitions into execution plans.
"""
from typing import Any
from collections import defaultdict

from .schemas import (
    CompileResult,
    CompileError,
    CompileWarning,
    NodeExecutionPlan,
    WorkflowExecutionPlan,
)
from .validators import (
    validate_dag,
    validate_credentials,
    validate_node_configs,
    validate_type_compatibility,
    topological_sort,
)


class WorkflowCompiler:
    """
    Compiles workflow JSON into an executable plan.
    
    Performs validation and generates execution order.
    
    Usage:
        compiler = WorkflowCompiler(workflow_data, user)
        result = compiler.compile()
        if result.success:
            plan = result.execution_plan
    """
    
    def __init__(self, workflow_data: dict, user, user_credentials: set[str] | None = None):
        """
        Initialize compiler.
        
        Args:
            workflow_data: Workflow definition with 'nodes' and 'edges'
            user: Django user object
            user_credentials: Optional set of user's credential IDs
        """
        self.nodes = workflow_data.get('nodes', [])
        self.edges = workflow_data.get('edges', [])
        self.settings = workflow_data.get('settings', {})
        self.user = user
        self.user_credentials = user_credentials or set()
        
        # Build lookup tables
        self._node_map = {node['id']: node for node in self.nodes}
        self._build_adjacency()
    
    def _build_adjacency(self):
        """Build adjacency lists from edges"""
        self._downstream: dict[str, list[str]] = defaultdict(list)
        self._upstream: dict[str, list[str]] = defaultdict(list)
        
        for edge in self.edges:
            source = edge.get('source')
            target = edge.get('target')
            if source and target:
                self._downstream[source].append(target)
                self._upstream[target].append(source)
    
    def compile(self) -> CompileResult:
        """
        Compile the workflow.
        
        Returns:
            CompileResult with success status, errors, and execution plan
        """
        errors: list[CompileError] = []
        warnings: list[CompileWarning] = []
        
        # Phase 1: DAG Validation
        dag_errors = validate_dag(self.nodes, self.edges)
        errors.extend(dag_errors)
        
        if errors:
            # Can't proceed if DAG is invalid
            return CompileResult(
                success=False,
                errors=errors,
                warnings=warnings,
                node_count=len(self.nodes),
                edge_count=len(self.edges),
            )
        
        # Phase 2: Credential Validation
        cred_errors = validate_credentials(self.nodes, self.user_credentials)
        errors.extend(cred_errors)
        
        # Phase 3: Node Config Validation
        config_errors = validate_node_configs(self.nodes)
        errors.extend(config_errors)
        
        # Phase 4: Type Compatibility Validation
        type_errors = validate_type_compatibility(self.nodes, self.edges)
        errors.extend(type_errors)
        
        if errors:
            return CompileResult(
                success=False,
                errors=errors,
                warnings=warnings,
                node_count=len(self.nodes),
                edge_count=len(self.edges),
            )
        
        # Build execution plan
        execution_plan = self._build_execution_plan()
        
        return CompileResult(
            success=True,
            errors=[],
            warnings=warnings,
            execution_plan=execution_plan.model_dump(),
            node_count=len(self.nodes),
            edge_count=len(self.edges),
        )
    
    def _build_execution_plan(self) -> WorkflowExecutionPlan:
        """Build the execution plan from validated workflow"""
        
        # Get topological order
        execution_order = topological_sort(self.nodes, self.edges)
        
        # Find entry points (trigger nodes)
        entry_points = [
            nid for nid in execution_order
            if not self._upstream.get(nid)
        ]
        
        # Build node execution plans
        node_plans: dict[str, NodeExecutionPlan] = {}
        
        for node in self.nodes:
            node_id = node['id']
            node_type = node.get('type', 'unknown')
            config = node.get('data', {}).get('config', {})
            
            # Get node-specific timeout or use default
            timeout = config.pop('timeout', None) or self.settings.get('node_timeout', 60)
            
            node_plans[node_id] = NodeExecutionPlan(
                node_id=node_id,
                node_type=node_type,
                config=config,
                dependencies=self._upstream.get(node_id, []),
                timeout_seconds=timeout,
            )
        
        # Get workflow ID from sources (if available)
        workflow_id = self.settings.get('workflow_id', 0)
        
        return WorkflowExecutionPlan(
            workflow_id=workflow_id,
            execution_order=execution_order,
            nodes=node_plans,
            entry_points=entry_points,
        )
    
    def get_node(self, node_id: str) -> dict | None:
        """Get a node by ID"""
        return self._node_map.get(node_id)
    
    def get_downstream(self, node_id: str) -> list[str]:
        """Get nodes that depend on this node's output"""
        return self._downstream.get(node_id, [])
    
    def get_upstream(self, node_id: str) -> list[str]:
        """Get nodes this node depends on"""
        return self._upstream.get(node_id, [])
