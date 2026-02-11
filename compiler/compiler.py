"""
Unified Workflow Compiler

Compiles workflow JSON directly into a LangGraph StateGraph in a single pass.
Eliminates intermediate execution plans for performance.
"""
import logging
from typing import Any, TypedDict
from uuid import UUID
from collections import defaultdict

from langgraph.graph import StateGraph, END
from langgraph.graph.state import CompiledStateGraph

from .schemas import (
    NodeExecutionPlan, # Keeping struct for internal use if needed, or we can use dicts
    ExecutionContext,
)
# We can use NodeExecutionPlan as a helper or just use dicts. Using dicts to minimalize allocs.

from .utils import get_node_type

from .validators import (
    validate_dag,
    validate_credentials,
    validate_node_configs,
    validate_type_compatibility,
    topological_sort,
)
from nodes.handlers.registry import get_registry
from logs.logger import get_execution_logger

logger = logging.getLogger(__name__)


class WorkflowCompilationError(Exception):
    """Base error for compilation failures"""
    def __init__(self, message: str, errors: list[Any] = None):
        super().__init__(message)
        self.errors = errors or []


class WorkflowState(TypedDict):
    """State schema for LangGraph workflow execution."""
    execution_id: str
    user_id: int
    workflow_id: int
    current_node: str
    node_outputs: dict[str, Any]
    variables: dict[str, Any]
    credentials: dict[str, Any]
    loop_stats: dict[str, int]
    error: str | None
    status: str
    nesting_depth: int
    workflow_chain: list[int]
    parent_execution_id: str | None
    timeout_budget_ms: int | None


class WorkflowCompiler:
    """
    Single-pass compiler that converts workflow JSON to executable StateGraph.
    """
    
    def __init__(self, workflow_data: dict, user=None, user_credentials: set[str] | None = None):
        self.workflow_data = workflow_data
        self.nodes = workflow_data.get('nodes', [])
        self.edges = workflow_data.get('edges', [])
        self.settings = workflow_data.get('settings', {}) or workflow_data.get('workflow_settings', {})
        self.user = user
        self.user_credentials = user_credentials or set()
        self.registry = get_registry()
        
        # Build adjacency for validation
        self._build_index()

    def _build_index(self):
        self._node_map = {n['id']: n for n in self.nodes}
        self._label_to_id = {n.get('data', {}).get('label', n['id']): n['id'] for n in self.nodes}
        # Secondary check for label in config
        for n in self.nodes:
            label = n.get('data', {}).get('label') or n.get('data', {}).get('config', {}).get('label')
            if label:
                self._label_to_id[label] = n['id']
            # Add node type as a fallback label if not already present
            node_type = get_node_type(n)
            if node_type:
                self._label_to_id.setdefault(node_type, n['id'])
                self._label_to_id.setdefault(node_type.lower(), n['id']) # Also add lowercase version

        # Pre-analyze expressions for each node
        self._node_expression_paths = {}
        for node in self.nodes:
            node_id = node['id']
            config = node.get('data', {}).get('config', node.get('data', {}))
            self._node_expression_paths[node_id] = self._get_expression_paths(config)

        self._outgoing = defaultdict(list)
        for edge in self.edges:
            src = edge.get('source')
            if src:
                self._outgoing[src].append(edge)

    def _get_expression_paths(self, config: Any, current_path: list = None) -> list[list]:
        """Recursively find paths to strings containing {{ }}."""
        if current_path is None:
            current_path = []
        
        paths = []
        if isinstance(config, dict):
            for k, v in config.items():
                paths.extend(self._get_expression_paths(v, current_path + [k]))
        elif isinstance(config, list):
            for i, v in enumerate(config):
                paths.extend(self._get_expression_paths(v, current_path + [i]))
        elif isinstance(config, str) and "{{" in config and "}}" in config:
            paths.append(current_path)
        
        return paths

    def compile(self, orchestrator: Any = None, supervision_level: Any = None) -> CompiledStateGraph:
        """
        Compile workflow directly to StateGraph.
        
        Args:
            orchestrator: Optional orchestrator instance for runtime hooks
            supervision_level: Level of supervision (FULL, ERROR_ONLY, NONE)
                - FULL: All hooks (before_node, after_node, on_error)
                - ERROR_ONLY: Only on_error hook called
                - NONE: No hooks called
            
        Returns:
            Executable CompiledStateGraph
            
        Raises:
            WorkflowCompilationError: If validation fails
        """
        # --- Validation Phase ---
        all_issues = []
        
        # 1. DAG Validation
        dag_errors = validate_dag(self.nodes, self.edges)
        hard_dag_errors = [e for e in dag_errors if e.type == "error"]
        if hard_dag_errors:
            raise WorkflowCompilationError("Invalid DAG structure", hard_dag_errors)
            
        # 2. Credential Validation
        all_issues.extend(validate_credentials(self.nodes, self.user_credentials))
        
        # 3. Config Validation
        all_issues.extend(validate_node_configs(self.nodes))
        
        # 4. Type Compatibility
        all_issues.extend(validate_type_compatibility(self.nodes, self.edges))
        
        # Only block on hard errors, not warnings (e.g. unknown output references)
        errors = [e for e in all_issues if e.type == "error"]
        if errors:
            raise WorkflowCompilationError("Workflow validation failed", errors)

        # --- Graph Construction Phase ---
        try:
            return self._build_graph(orchestrator, supervision_level)
        except Exception as e:
            logger.exception("Graph construction failed")
            raise WorkflowCompilationError(f"Graph construction failed: {str(e)}")

    def _build_graph(self, orchestrator: Any, supervision_level: Any) -> CompiledStateGraph:
        graph = StateGraph(WorkflowState)
        
        # 1. Determine Execution Order (for linear edges fallback)
        # Note: LangGraph doesn't strictly need topo sort, but we use it to identify entry points
        # and ensure graph integrity.
        topo_order = topological_sort(self.nodes, self.edges)
        
        # 2. Add Nodes
        for node in self.nodes:
            node_id = node['id']
            # Create handler function (closure)
            node_func = self._create_node_function(node, orchestrator, supervision_level)
            graph.add_node(node_id, node_func)
            
        # 3. Add Edges
        conditional_nodes = {'if', 'switch', 'loop', 'split_in_batches', 'if_condition'}
        
        for node in self.nodes:
            node_id = node['id']
            node_type = get_node_type(node)
            edges = self._outgoing[node_id]
            
            if not edges:
                graph.add_edge(node_id, END)
                continue
                
            if node_type in conditional_nodes:
                self._add_conditional_edges(graph, node_id, edges)
            else:
                for edge in edges:
                    target = edge.get('target')
                    if target:
                        graph.add_edge(node_id, target)
                        
        # 4. Set Entry Point
        # Entry points are nodes with no upstream dependencies (or specifically marked)
        # We use topological sort result; the first items are usually entry points.
        # But specifically those with in-degree 0.
        # Let's find nodes that are NOT targets of any edge.
        targets = {e['target'] for e in self.edges if e.get('target')}
        entry_points = [n['id'] for n in self.nodes if n['id'] not in targets]
        
        if not entry_points:
             # Fallback if circular or weird (should be caught by DAG check)
             entry_points = [topo_order[0]] if topo_order else []
             
        for entry in entry_points:
            graph.set_entry_point(entry)
            
        return graph.compile()

    def _create_node_function(self, node_data: dict, orchestrator: Any, supervision_level: Any):
        node_id = node_data['id']
        node_type = get_node_type(node_data)
        config = node_data.get('data', {}) # .get('config')? Frontends vary. Assuming data IS config or contains it.
        # Normalizing config:
        # If 'data' has 'config', use that. Else use 'data'.
        node_config = config.get('config', config)
        
        # Basic timeout handling
        timeout = node_config.get('timeout', self.settings.get('node_timeout', 60))
        
        registry = self.registry

        async def node_function(state: WorkflowState) -> WorkflowState:
            import asyncio
            from orchestrator.interface import AbortDecision, PauseDecision

            state['current_node'] = node_id
            execution_id = UUID(state['execution_id']) if isinstance(state['execution_id'], str) else state['execution_id']
            
            # loop_stats init
            if 'loop_stats' not in state or state['loop_stats'] is None:
                state['loop_stats'] = {}

            if state.get('status') in ['failed', 'cancelled', 'paused']:
                return state

            # Before Hook (only for FULL supervision)
            # Import here to avoid circular import
            from orchestrator.interface import SupervisionLevel
            
            should_call_before = (
                orchestrator and 
                supervision_level not in (SupervisionLevel.ERROR_ONLY, SupervisionLevel.NONE, 'error_only', 'none')
            )
            
            if should_call_before:
                decision = await orchestrator.before_node(execution_id, node_id, state)
                if isinstance(decision, AbortDecision):
                    state['status'] = 'failed'
                    state['error'] = decision.reason
                    return state
                if isinstance(decision, PauseDecision):
                    state['status'] = 'paused'
                    return state

            try:
                if not registry.has_handler(node_type):
                     raise ValueError(f"Unknown node type: {node_type}")
                
                handler = registry.get_handler(node_type)
                
                # 1. Create Context (Empty Initial Input)
                context = ExecutionContext(
                    execution_id=execution_id,
                    user_id=state['user_id'],
                    workflow_id=state['workflow_id'],
                    node_outputs=state['node_outputs'],
                    credentials=state['credentials'],
                    variables=state['variables'],
                    current_node_id=node_id,
                    loop_stats=state['loop_stats'],
                    node_label_to_id=self._label_to_id,
                    nesting_depth=state.get('nesting_depth', 0),
                    workflow_chain=state.get('workflow_chain', []),
                    parent_execution_id=state.get('parent_execution_id'),
                    timeout_budget_ms=state.get('timeout_budget_ms'),
                    current_input=[],
                )
                
                # 2. Resolve Input Items
                items = context.get_input_for_node(node_id, self.edges)
                context.current_input = items
                
                # 3. Resolve Expressions in Config
                expr_paths = self._node_expression_paths.get(node_id, [])
                resolved_config = context.resolve_expressions(node_config, expr_paths)
                
                # 4. Consolidate input data for handler
                input_data = {}
                if items:
                    first_item = items[0]
                    if isinstance(first_item, dict):
                        input_data.update(first_item.get("json", first_item))

                if f"_input_{node_id}" in state['node_outputs']:
                    injected_input = state['node_outputs'][f"_input_{node_id}"]
                    if isinstance(injected_input, dict):
                        input_data.update(injected_input)
                    elif isinstance(injected_input, list) and injected_input:
                        input_data.update(injected_input[0].get("json", injected_input[0]))

                # Log start
                logger_instance = get_execution_logger()
                await logger_instance.log_node_start(
                    execution_id=execution_id,
                    node_id=node_id,
                    node_type=node_type,
                    node_name=node_id, # Could look up label if available
                    input_data={'items': input_data}, # Wrap in dict for consistency
                    config=node_config
                )

                # Execute
                start_time = asyncio.get_event_loop().time()
                try:
                    result = await asyncio.wait_for(
                        handler.execute(input_data, resolved_config, context),
                        timeout=timeout
                    )
                    duration = (asyncio.get_event_loop().time() - start_time) * 1000
                    
                    # Serialize results for state storage (and next nodes)
                    serialized_items = [item.model_dump(by_alias=True) for item in result.items]
                    
                    # Update state
                    state['node_outputs'][node_id] = serialized_items
                    state['node_outputs'][f"_handle_{node_id}"] = result.output_handle
                    
                    # Log completion
                    await logger_instance.log_node_complete(
                        execution_id=execution_id,
                        node_id=node_id,
                        success=result.success,
                        output_data={'items': serialized_items},
                        error_message=result.error or '',
                        duration_ms=int(duration),
                        warnings=[w.model_dump(by_alias=True) for w in context.warnings]
                    )
                except Exception as e:
                    # Log error
                    duration = (asyncio.get_event_loop().time() - start_time) * 1000
                    await logger_instance.log_node_complete(
                        execution_id=execution_id,
                        node_id=node_id,
                        success=False,
                        output_data={},
                        error_message=str(e),
                        duration_ms=int(duration)
                    )
                    raise e # Re-raise to be caught by outer try/except for supervision handling
                
                # Track loop iterations
                if node_type in ['loop', 'split_in_batches']:
                    state['loop_stats'][node_id] = state['loop_stats'].get(node_id, 0) + 1
                
                # Check if this node feeds back to a loop node - accumulate results
                # This enables the loop node to return all accumulated results when done
                for edge in self.edges:
                    if edge.get('source') == node_id:
                        target = edge.get('target')
                        target_type = self._node_map.get(target, {}).get('type')
                        if target_type in ['loop', 'split_in_batches']:
                            # This node's output feeds a loop - accumulate for the loop node
                            acc_key = f"_accumulated_{target}"
                            if acc_key not in state['variables']:
                                state['variables'][acc_key] = []
                            # We might need to restructure how loop accumulation works with items
                            # For now just appending the serialized items
                            # But wait, look accumulation logic was outside result.success check before
                            # Moving loops logic to AFTER success check is better anyway.
                            # But I need to be careful not to introduce bugs if loop accumulation logic was intended for partial results? 
                            # Usually only successful executions produce data worth accumulating.
                            pass # Logic moved inside success block

                if not result.success:
                    # on_error called for FULL and ERROR_ONLY supervision
                    should_call_error = (
                        orchestrator and 
                        supervision_level not in (SupervisionLevel.NONE, 'none')
                    )
                    if should_call_error:
                        err_decision = await orchestrator.on_error(execution_id, node_id, result.error, state)
                        if isinstance(err_decision, AbortDecision):
                            state['error'] = result.error
                            state['status'] = 'failed'
                        # Retry not implemented in this reduced scope
                    else:
                        # No orchestrator or NONE mode - just fail
                        state['error'] = result.error
                        state['status'] = 'failed'
                else:
                    # Check if this node feeds back to a loop node - accumulate results
                    # This enables the loop node to return all accumulated results when done
                    for edge in self.edges:
                        if edge.get('source') == node_id:
                            target = edge.get('target')
                            target_type = self._node_map.get(target, {}).get('type')
                            if target_type in ['loop', 'split_in_batches']:
                                # This node's output feeds a loop - accumulate for the loop node
                                acc_key = f"_accumulated_{target}"
                                if acc_key not in state['variables']:
                                    state['variables'][acc_key] = []
                                state['variables'][acc_key].append(serialized_items)

                    # after_node only for FULL supervision
                    should_call_after = (
                        orchestrator and 
                        supervision_level not in (SupervisionLevel.ERROR_ONLY, SupervisionLevel.NONE, 'error_only', 'none')
                    )
                    if should_call_after:
                        post_decision = await orchestrator.after_node(
                            execution_id, node_id, 
                            {'items': serialized_items, 'output_handle': result.output_handle}, 
                            state
                        )
                        if isinstance(post_decision, AbortDecision):
                            state['status'] = 'failed'
                            state['error'] = post_decision.reason
                        elif isinstance(post_decision, PauseDecision):
                            state['status'] = 'paused'

            except Exception as e:
                state['error'] = f"Node {node_id} error: {str(e)}"
                state['status'] = 'failed'
                logger.exception(f"Node execution failed: {node_id}")

            return state

        return node_function

    def _add_conditional_edges(self, graph, node_id, edges):
        handle_to_target = {}
        for edge in edges:
            handle = edge.get('sourceHandle', 'default') # Default string if missing?
            # Frontends often use 'true'/'false' or 'loop'/'done' or 'default'
            # If sourceHandle is missing, assume it matches standard output
            target = edge.get('target')
            handle_to_target[handle] = target

        def route(state: WorkflowState) -> str:
            handle = state['node_outputs'].get(f"_handle_{node_id}", 'default')
            # Fallback for old nodes that don't return handle?
            # Or if handle is not in map?
            
            tgt = handle_to_target.get(handle)
            if tgt:
                return tgt
            
            # Fallback: if 'default' exists in map?
            if 'default' in handle_to_target:
                return handle_to_target['default']
                
            return END

        graph.add_conditional_edges(node_id, route, handle_to_target)
