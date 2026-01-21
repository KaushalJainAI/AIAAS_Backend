"""
LangGraph Builder

Converts compiled WorkflowExecutionPlan into a LangGraph StateGraph.
"""
import logging
from typing import Any, TypedDict
from uuid import UUID

from langgraph.graph import StateGraph, END
from langgraph.graph.state import CompiledStateGraph

from .schemas import WorkflowExecutionPlan, NodeExecutionPlan, ExecutionContext
from nodes.handlers.registry import get_registry

logger = logging.getLogger(__name__)


class WorkflowState(TypedDict):
    """State schema for LangGraph workflow execution."""
    execution_id: str
    user_id: int
    workflow_id: int
    current_node: str
    node_outputs: dict[str, Any]
    variables: dict[str, Any]
    credentials: dict[str, Any]
    error: str | None
    status: str  # 'running', 'completed', 'failed', 'cancelled'


class LangGraphBuilder:
    """
    Convert compiled WorkflowExecutionPlan into LangGraph StateGraph.
    
    Creates a graph where:
    - Each node is a function that executes a workflow node
    - Edges represent data flow (with conditionals for If/Switch)
    - State carries execution context between nodes
    
    Usage:
        builder = LangGraphBuilder()
        graph = builder.build(execution_plan, edges)
        result = graph.invoke({"execution_id": "...", ...})
    """
    
    def __init__(self):
        self.registry = get_registry()
    
    def build(
        self,
        execution_plan: WorkflowExecutionPlan,
        edges: list[dict]
    ) -> CompiledStateGraph:
        """
        Build a LangGraph StateGraph from an execution plan.
        
        Args:
            execution_plan: Compiled execution plan
            edges: Original workflow edges for routing
            
        Returns:
            Compiled LangGraph StateGraph ready for invocation
        """
        # Create the state graph with our state schema
        graph = StateGraph(WorkflowState)
        
        # Build edge index for downstream lookup
        edge_index: dict[str, list[dict]] = {}
        for edge in edges:
            source = edge.get('source')
            if source not in edge_index:
                edge_index[source] = []
            edge_index[source].append(edge)
        
        # Add nodes to the graph
        for node_id, node_plan in execution_plan.nodes.items():
            node_func = self._create_node_function(node_plan)
            graph.add_node(node_id, node_func)
        
        # Add edges based on execution order and conditionals
        conditional_nodes = {'if', 'switch'}
        
        for node_id, node_plan in execution_plan.nodes.items():
            downstream_edges = edge_index.get(node_id, [])
            
            if not downstream_edges:
                # Node has no outgoing edges - go to END
                graph.add_edge(node_id, END)
            elif node_plan.node_type in conditional_nodes:
                # Conditional node - add conditional edges
                self._add_conditional_edges(graph, node_id, downstream_edges)
            else:
                # Regular node - add edges to all downstream nodes
                for edge in downstream_edges:
                    target = edge.get('target')
                    graph.add_edge(node_id, target)
        
        # Set entry points
        for entry_point in execution_plan.entry_points:
            graph.set_entry_point(entry_point)
        
        # Compile and return
        return graph.compile()
    
    def _create_node_function(self, node_plan: NodeExecutionPlan):
        """
        Create a function that executes a node and updates state.
        
        Args:
            node_plan: The execution plan for this node
            
        Returns:
            Async function compatible with LangGraph
        """
        node_id = node_plan.node_id
        node_type = node_plan.node_type
        config = node_plan.config
        timeout = node_plan.timeout_seconds
        registry = self.registry
        
        async def node_function(state: WorkflowState) -> WorkflowState:
            """Execute the node and update state."""
            import asyncio
            
            # Update current node
            state['current_node'] = node_id
            
            # Check if already failed
            if state.get('status') == 'failed':
                return state
            
            try:
                # Get handler
                if not registry.has_handler(node_type):
                    state['error'] = f"Unknown node type: {node_type}"
                    state['status'] = 'failed'
                    return state
                
                handler = registry.get_handler(node_type)
                
                # Build execution context
                context = ExecutionContext(
                    execution_id=UUID(state['execution_id']),
                    user_id=state['user_id'],
                    workflow_id=state['workflow_id'],
                    node_outputs=state['node_outputs'],
                    credentials=state['credentials'],
                    variables=state['variables'],
                    current_node_id=node_id,
                )
                
                # Get input from previous nodes
                input_data = state['node_outputs'].get(f"_input_{node_id}", {})
                
                # Execute with timeout
                result = await asyncio.wait_for(
                    handler.execute(input_data, config, context),
                    timeout=timeout
                )
                
                # Store output
                state['node_outputs'][node_id] = result.data
                state['node_outputs'][f"_handle_{node_id}"] = result.output_handle
                
                if not result.success:
                    logger.warning(f"Node {node_id} failed: {result.error}")
                
            except asyncio.TimeoutError:
                state['error'] = f"Node {node_id} timed out after {timeout}s"
                state['status'] = 'failed'
            except Exception as e:
                state['error'] = f"Node {node_id} error: {str(e)}"
                state['status'] = 'failed'
                logger.exception(f"Error executing node {node_id}")
            
            return state
        
        return node_function
    
    def _add_conditional_edges(
        self,
        graph: StateGraph,
        node_id: str,
        downstream_edges: list[dict]
    ) -> None:
        """
        Add conditional edges for If/Switch nodes.
        
        Args:
            graph: The StateGraph to add edges to
            node_id: The conditional node ID
            downstream_edges: List of edges from this node
        """
        # Build routing map: handle -> target node
        handle_to_target = {}
        for edge in downstream_edges:
            handle = edge.get('sourceHandle', 'output')
            target = edge.get('target')
            handle_to_target[handle] = target
        
        # Create routing function
        def route_conditional(state: WorkflowState) -> str:
            """Route based on which output handle was used."""
            output_handle = state['node_outputs'].get(f"_handle_{node_id}", 'output')
            target = handle_to_target.get(output_handle)
            
            if target:
                return target
            
            # Default to first available target or END
            if handle_to_target:
                return next(iter(handle_to_target.values()))
            return END
        
        # Add conditional edges
        graph.add_conditional_edges(
            node_id,
            route_conditional,
            handle_to_target
        )


def build_langgraph(
    execution_plan: WorkflowExecutionPlan,
    edges: list[dict]
) -> CompiledStateGraph:
    """
    Convenience function to build a LangGraph from an execution plan.
    
    Args:
        execution_plan: Compiled execution plan
        edges: Original workflow edges
        
    Returns:
        Compiled LangGraph StateGraph
    """
    builder = LangGraphBuilder()
    return builder.build(execution_plan, edges)
