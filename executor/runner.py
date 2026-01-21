"""
Workflow Executor - Node Runner and Workflow Execution

Core execution engine for running compiled workflows node-by-node.

Architecture:
- NodeRunner: Executes individual nodes with timeout and error handling
- WorkflowExecutor: Orchestrates the full workflow following execution plan
- Data flows between nodes via ExecutionContext.node_outputs

Usage:
    executor = WorkflowExecutor(workflow, execution_plan, logger)
    result = await executor.execute(input_data, context)
"""
import asyncio
import logging
import traceback
from datetime import datetime
from typing import Any
from uuid import UUID

from compiler.schemas import (
    ExecutionContext,
    NodeExecutionPlan,
    WorkflowExecutionPlan,
)
from nodes.handlers.base import NodeExecutionResult
from nodes.handlers.registry import get_registry

logger = logging.getLogger(__name__)


# ==================== Node Runner ====================

class NodeRunner:
    """
    Runs individual nodes with timeout and error handling.
    
    Responsibilities:
    - Fetch the appropriate handler from registry
    - Execute with configurable timeout
    - Catch and wrap errors
    - Return standardized NodeExecutionResult
    
    Example:
        runner = NodeRunner()
        result = await runner.run(
            node_id="node_1",
            node_type="http_request",
            config={"url": "https://api.example.com"},
            input_data={"key": "value"},
            context=execution_context,
            timeout_seconds=60
        )
    """
    
    def __init__(self):
        self.registry = get_registry()
    
    async def run(
        self,
        node_id: str,
        node_type: str,
        config: dict[str, Any],
        input_data: dict[str, Any],
        context: ExecutionContext,
        timeout_seconds: int = 60,
        max_retries: int = 0,
        retry_delay_seconds: int = 5,
        on_error: callable = None
    ) -> NodeExecutionResult:
        """
        Execute a single node with timeout, retry logic, and error handling.
        
        Args:
            node_id: Unique identifier for this node instance
            node_type: Type of node (e.g., 'http_request', 'code')
            config: Node configuration (field values)
            input_data: Data from upstream nodes
            context: Execution context with credentials, variables
            timeout_seconds: Maximum execution time per attempt
            max_retries: Number of retry attempts (0 = no retries)
            retry_delay_seconds: Delay between retry attempts
            on_error: Optional callback(node_id, error, attempt) for error streaming
            
        Returns:
            NodeExecutionResult with success status and output data
        """
        logger.info(f"Running node {node_id} (type: {node_type})")
        
        # Update context with current node
        context.current_node_id = node_id
        
        # Get handler from registry
        if not self.registry.has_handler(node_type):
            error_result = NodeExecutionResult(
                success=False,
                error=f"Unknown node type: {node_type}",
                output_handle="error"
            )
            if on_error:
                on_error(node_id, error_result.error, 0)
            return error_result
        
        handler = self.registry.get_handler(node_type)
        
        # Validate configuration
        config_errors = handler.validate_config(config)
        if config_errors:
            error_result = NodeExecutionResult(
                success=False,
                error=f"Invalid config: {', '.join(config_errors)}",
                output_handle="error"
            )
            if on_error:
                on_error(node_id, error_result.error, 0)
            return error_result
        
        # Retry loop
        last_error = None
        for attempt in range(max_retries + 1):
            try:
                if attempt > 0:
                    logger.info(f"Retrying node {node_id}, attempt {attempt + 1}/{max_retries + 1}")
                    await asyncio.sleep(retry_delay_seconds)
                
                # Execute with timeout
                result = await asyncio.wait_for(
                    handler.execute(input_data, config, context),
                    timeout=timeout_seconds
                )
                
                if result.success:
                    logger.info(
                        f"Node {node_id} completed: success={result.success}, "
                        f"handle={result.output_handle}"
                    )
                    return result
                
                # Node returned failure
                last_error = result.error
                if on_error:
                    on_error(node_id, result.error, attempt)
                
                # Check if this is a retryable error
                if attempt < max_retries:
                    logger.warning(f"Node {node_id} failed (attempt {attempt + 1}): {result.error}")
                else:
                    return result
                    
            except asyncio.TimeoutError:
                last_error = f"Node {node_id} timed out after {timeout_seconds}s"
                logger.error(f"{last_error} (attempt {attempt + 1})")
                if on_error:
                    on_error(node_id, last_error, attempt)
                    
            except Exception as e:
                last_error = f"Node {node_id} failed: {str(e)}"
                logger.exception(f"{last_error} (attempt {attempt + 1})")
                if on_error:
                    on_error(node_id, last_error, attempt)
        
        # All retries exhausted
        error_msg = f"Node {node_id} failed after {max_retries + 1} attempts: {last_error}"
        logger.error(error_msg)
        return NodeExecutionResult(
            success=False,
            error=error_msg,
            data={
                "traceback": traceback.format_exc() if last_error else "",
                "attempts": max_retries + 1
            },
            output_handle="error"
        )


# ==================== Workflow Executor ====================

class WorkflowExecutor:
    """
    Orchestrates workflow execution following the execution plan.
    
    Responsibilities:
    - Execute nodes in topological order
    - Pass data between nodes via ExecutionContext
    - Handle conditional branching (If, Switch nodes)
    - Track execution progress
    - Coordinate with ExecutionLogger for logging
    
    Example:
        executor = WorkflowExecutor(execution_plan, edges, logger)
        final_output, status = await executor.execute(input_data, context)
    """
    
    def __init__(
        self,
        execution_plan: WorkflowExecutionPlan,
        edges: list[dict],
        execution_logger: 'ExecutionLogger | None' = None
    ):
        """
        Initialize the workflow executor.
        
        Args:
            execution_plan: Compiled execution plan with node order
            edges: Original workflow edges for data routing
            execution_logger: Optional logger for recording execution
        """
        self.plan = execution_plan
        self.edges = edges
        self.logger = execution_logger
        self.runner = NodeRunner()
        
        # Build edge lookup for faster routing decisions
        self._build_edge_index()
    
    def _build_edge_index(self) -> None:
        """Build index for quick edge lookups."""
        # Edges by source: source_id -> [(target_id, source_handle)]
        self.edges_by_source: dict[str, list[tuple[str, str]]] = {}
        
        for edge in self.edges:
            source = edge.get("source")
            target = edge.get("target")
            source_handle = edge.get("sourceHandle", "output")
            
            if source not in self.edges_by_source:
                self.edges_by_source[source] = []
            self.edges_by_source[source].append((target, source_handle))
    
    async def execute(
        self,
        input_data: dict[str, Any],
        context: ExecutionContext
    ) -> tuple[dict[str, Any], str]:
        """
        Execute the full workflow.
        
        Args:
            input_data: Initial input data for trigger nodes
            context: Execution context
            
        Returns:
            Tuple of (final output data, status string)
            Status: 'completed', 'failed', 'cancelled'
        """
        logger.info(
            f"Starting workflow execution: {self.plan.workflow_id}, "
            f"nodes: {len(self.plan.execution_order)}"
        )
        
        final_output = {}
        nodes_executed = 0
        
        # Set to track nodes that should be skipped (conditional branching)
        skip_nodes: set[str] = set()
        
        try:
            for node_id in self.plan.execution_order:
                # Skip if marked (from conditional routing)
                if node_id in skip_nodes:
                    logger.debug(f"Skipping node {node_id} (conditional branch)")
                    if self.logger:
                        self.logger.log_node_skip(
                            context.execution_id,
                            node_id,
                            "Skipped by conditional routing"
                        )
                    continue
                
                # Get node execution plan
                node_plan = self.plan.nodes.get(node_id)
                if not node_plan:
                    logger.warning(f"No execution plan for node {node_id}")
                    continue
                
                # Gather input from upstream nodes
                node_input = context.get_input_for_node(node_id, self.edges)
                
                # For entry points (triggers), merge with initial input
                if node_id in self.plan.entry_points:
                    node_input = {**input_data, **node_input}
                
                # Log node start
                start_time = datetime.utcnow()
                if self.logger:
                    self.logger.log_node_start(
                        context.execution_id,
                        node_id,
                        node_plan.node_type,
                        node_plan.config.get("name", node_id),
                        node_input
                    )
                
                # Execute the node
                result = await self.runner.run(
                    node_id=node_id,
                    node_type=node_plan.node_type,
                    config=node_plan.config,
                    input_data=node_input,
                    context=context,
                    timeout_seconds=node_plan.timeout_seconds
                )
                
                # Calculate duration
                duration_ms = int(
                    (datetime.utcnow() - start_time).total_seconds() * 1000
                )
                
                # Log node completion
                if self.logger:
                    self.logger.log_node_complete(
                        context.execution_id,
                        node_id,
                        result.success,
                        result.data,
                        result.error,
                        duration_ms
                    )
                
                if not result.success:
                    # Node failed - check if we should continue
                    logger.error(f"Node {node_id} failed: {result.error}")
                    
                    # Route to error path if any
                    self._route_conditional(node_id, result.output_handle, skip_nodes)
                    
                    # Check if error is fatal (no downstream on error path)
                    if not self._has_downstream_on_handle(node_id, "error"):
                        return {
                            "error": result.error,
                            "failed_node": node_id,
                            "output": result.data
                        }, "failed"
                
                # Store output in context
                context.set_node_output(node_id, result.data)
                final_output = result.data
                nodes_executed += 1
                
                # Handle conditional routing (If, Switch nodes)
                self._route_conditional(node_id, result.output_handle, skip_nodes)
            
            logger.info(
                f"Workflow completed: {nodes_executed} nodes executed"
            )
            return final_output, "completed"
            
        except asyncio.CancelledError:
            logger.warning("Workflow execution cancelled")
            return {"cancelled": True}, "cancelled"
            
        except Exception as e:
            logger.exception(f"Workflow execution failed: {e}")
            return {
                "error": str(e),
                "traceback": traceback.format_exc()
            }, "failed"
    
    def _route_conditional(
        self,
        node_id: str,
        output_handle: str,
        skip_nodes: set[str]
    ) -> None:
        """
        Handle conditional routing for If/Switch nodes.
        
        Marks nodes on non-taken branches for skipping.
        """
        downstream = self.edges_by_source.get(node_id, [])
        
        if len(downstream) <= 1:
            # No branching, nothing to do
            return
        
        # Multiple downstream - this is a conditional node
        for target_id, source_handle in downstream:
            if source_handle != output_handle:
                # This path was not taken - skip all downstream
                self._mark_branch_for_skip(target_id, skip_nodes)
    
    def _mark_branch_for_skip(
        self,
        start_node: str,
        skip_nodes: set[str]
    ) -> None:
        """Recursively mark a branch of nodes to skip."""
        if start_node in skip_nodes:
            return
        
        skip_nodes.add(start_node)
        
        # Also skip downstream nodes
        for target_id, _ in self.edges_by_source.get(start_node, []):
            self._mark_branch_for_skip(target_id, skip_nodes)
    
    def _has_downstream_on_handle(self, node_id: str, handle: str) -> bool:
        """Check if node has any downstream connections on a specific handle."""
        for target_id, source_handle in self.edges_by_source.get(node_id, []):
            if source_handle == handle:
                return True
        return False
