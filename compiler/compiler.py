"""
Unified Workflow Compiler.

Single-pass conversion from a ReactFlow-shaped workflow JSON to a compiled
LangGraph StateGraph.

Pipeline:
    __init__   → index nodes, pre-analyse expression paths, map edges.
    compile()  → validate → build graph → return compiled StateGraph.

The heavy lifting for a single node (context init, expression resolution,
handler dispatch, orchestrator hooks, logging, accumulator gating) lives
in _create_node_function, which returns an async closure invoked by
LangGraph for each node.
"""
from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from typing import Any, Callable, TypedDict
from uuid import UUID

from langgraph.graph import StateGraph, END, START
from langgraph.graph.state import CompiledStateGraph

from .schemas import (
    NodeExecutionPlan,  # Re-exported for back-compat with external callers.
    ExecutionContext,
)
from .utils import get_node_type
from .config_access import get_node_config, get_node_data
from .node_types import CONDITIONAL_NODE_TYPES, LOOP_NODE_TYPES
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

# Hook timeout: orchestrator LLM calls can be slow; 5 min is generous but caps
# runaway prompts. Per-node handler timeout is separate (see node_config.timeout).
_ORCHESTRATOR_HOOK_TIMEOUT_S = 300
# Default per-node execution timeout when none is configured. 5 min accommodates
# typical LLM + HTTP chains; anything longer should be explicit.
_DEFAULT_NODE_TIMEOUT_S = 300


class WorkflowCompilationError(Exception):
    """Raised when validation fails or graph construction blows up."""
    def __init__(self, message: str, errors: list[Any] | None = None):
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
    skills: list[dict]


class WorkflowCompiler:
    """Convert workflow JSON to an executable LangGraph StateGraph."""

    def __init__(
        self,
        workflow_data: dict,
        user=None,
        user_credentials: set[str] | None = None,
    ):
        self.workflow_data = workflow_data
        self.nodes: list[dict] = workflow_data.get("nodes", []) or []
        self.edges: list[dict] = workflow_data.get("edges", []) or []
        self.settings: dict = (
            workflow_data.get("settings")
            or workflow_data.get("workflow_settings")
            or {}
        )
        self.user = user
        self.user_credentials = user_credentials or set()
        self.registry = get_registry()

        self._build_index()

    # -- indexing -----------------------------------------------------------

    def _build_index(self) -> None:
        """
        Build the lookup tables consumed by compile() and the per-node closures:
            _node_map                node_id → node dict
            _label_to_id             user label / type / id → node_id
            _outgoing                node_id → outgoing edges
            _node_expression_paths   node_id → list of paths with {{ }}
            _loop_body_sources       loop_node_id → set of sources whose edges
                                     into the loop are body-return edges
        """
        self._node_map: dict[str, dict] = {n["id"]: n for n in self.nodes}

        self._label_to_id: dict[str, str] = {}
        for n in self.nodes:
            data = get_node_data(n)
            for candidate in (data.get("label"), get_node_config(n).get("label")):
                if candidate:
                    self._label_to_id.setdefault(candidate, n["id"])
            # Fallback: raw id and type name (+ lowercase) map to the id.
            self._label_to_id.setdefault(n["id"], n["id"])
            ntype = get_node_type(n)
            if ntype:
                self._label_to_id.setdefault(ntype, n["id"])
                self._label_to_id.setdefault(ntype.lower(), n["id"])

        self._node_expression_paths: dict[str, list[list]] = {
            n["id"]: _find_expression_paths(get_node_config(n)) for n in self.nodes
        }

        self._outgoing: dict[str, list[dict]] = defaultdict(list)
        for edge in self.edges:
            src = edge.get("source")
            if src:
                self._outgoing[src].append(edge)

        self._loop_body_sources = _compute_loop_body_sources(self.nodes, self.edges)

    # -- public API ---------------------------------------------------------

    def compile(
        self,
        orchestrator: Any = None,
        supervision_level: Any = None,
    ) -> CompiledStateGraph:
        """Validate, build, and return a compiled StateGraph."""
        all_issues: list = []

        dag_errors = validate_dag(self.nodes, self.edges)
        hard = [e for e in dag_errors if e.type == "error"]
        if hard:
            raise WorkflowCompilationError("Invalid DAG structure", hard)

        all_issues.extend(validate_credentials(self.nodes, self.user_credentials))
        all_issues.extend(validate_node_configs(self.nodes))
        all_issues.extend(validate_type_compatibility(self.nodes, self.edges))

        errors = [e for e in all_issues if e.type == "error"]
        if errors:
            raise WorkflowCompilationError("Workflow validation failed", errors)

        try:
            return self._build_graph(orchestrator, supervision_level)
        except Exception as e:
            logger.exception("Graph construction failed")
            raise WorkflowCompilationError(f"Graph construction failed: {e}")

    # -- graph construction -------------------------------------------------

    def _build_graph(
        self, orchestrator: Any, supervision_level: Any,
    ) -> CompiledStateGraph:
        graph: StateGraph = StateGraph(WorkflowState)

        for node in self.nodes:
            graph.add_node(
                node["id"],
                self._create_node_function(node, orchestrator, supervision_level),
            )

        for node in self.nodes:
            node_id = node["id"]
            ntype = get_node_type(node)
            edges = self._outgoing[node_id]

            if not edges:
                graph.add_edge(node_id, END)
                continue

            if ntype in CONDITIONAL_NODE_TYPES:
                self._add_conditional_edges(graph, node_id, edges)
            else:
                for edge in edges:
                    if edge.get("target"):
                        graph.add_edge(node_id, edge["target"])

        self._wire_entry_points(graph)
        return graph.compile()

    def _wire_entry_points(self, graph: StateGraph) -> None:
        """
        Wire every zero-in-degree node as a parallel entry point.

        LangGraph supports this via add_edge(START, n). The previous code
        called set_entry_point in a loop, which only kept the last value —
        silently dropping additional triggers.
        """
        targets = {e["target"] for e in self.edges if e.get("target")}
        entry_points = [n["id"] for n in self.nodes if n["id"] not in targets]

        if not entry_points:
            # Defensive fallback — DAG validation should have rejected this.
            topo = topological_sort(self.nodes, self.edges)
            entry_points = [topo[0]] if topo else []

        for entry in entry_points:
            graph.add_edge(START, entry)

    def _add_conditional_edges(
        self, graph: StateGraph, node_id: str, edges: list[dict],
    ) -> None:
        """
        Route from a conditional node using `sourceHandle` of the outgoing edge.

        LangGraph's add_conditional_edges(path_map) must include EVERY value
        the router function can return — including END. Previously END fell
        through without being in the map, which raised an error at runtime.
        """
        handle_to_target: dict[str, str] = {}
        for edge in edges:
            handle = edge.get("sourceHandle") or "default"
            target = edge.get("target")
            if target:
                handle_to_target[handle] = target

        def route(state: WorkflowState) -> str:
            handle = state["node_outputs"].get(f"_handle_{node_id}", "default")
            tgt = handle_to_target.get(handle)
            if tgt:
                return tgt
            # Unknown handle → try default, then terminate.
            return handle_to_target.get("default", END)

        # Router may legitimately return END; include it in the path map.
        path_map: dict[str, str] = dict(handle_to_target)
        path_map[END] = END
        graph.add_conditional_edges(node_id, route, path_map)

    # -- per-node closure ---------------------------------------------------

    def _create_node_function(
        self, node_data: dict, orchestrator: Any, supervision_level: Any,
    ) -> Callable:
        node_id = node_data["id"]
        node_type = get_node_type(node_data)
        node_config = get_node_config(node_data)

        # customFieldDefs lives at data-level (not inside data.config); merge
        # it in so structured-output handlers find it at one known location.
        data_level = get_node_data(node_data)
        if "customFieldDefs" in data_level and "customFieldDefs" not in node_config:
            node_config = {**node_config, "customFieldDefs": data_level["customFieldDefs"]}

        timeout = node_config.get(
            "timeout", self.settings.get("node_timeout", _DEFAULT_NODE_TIMEOUT_S),
        )
        edges_for_closure = self.edges
        outgoing_edges = self._outgoing[node_id]
        loop_body_sources = self._loop_body_sources
        expr_paths = self._node_expression_paths.get(node_id, [])
        registry = self.registry
        label_to_id = self._label_to_id

        # Imported inside the closure to avoid top-level circular imports.
        async def node_function(state: WorkflowState) -> WorkflowState:
            from orchestrator.interface import (
                AbortDecision, PauseDecision, SupervisionLevel,
            )

            state["current_node"] = node_id
            execution_id = (
                UUID(state["execution_id"])
                if isinstance(state["execution_id"], str)
                else state["execution_id"]
            )
            if state.get("loop_stats") is None:
                state["loop_stats"] = {}

            if state.get("status") in ("failed", "cancelled", "paused"):
                return state

            log = get_execution_logger()

            # 1. Build execution context. Failure here is fatal for this node.
            try:
                context = ExecutionContext(
                    execution_id=execution_id,
                    user_id=state["user_id"],
                    workflow_id=state["workflow_id"],
                    node_outputs=state["node_outputs"],
                    credentials=state["credentials"],
                    variables=state["variables"],
                    current_node_id=node_id,
                    loop_stats=state["loop_stats"],
                    node_label_to_id=label_to_id,
                    nesting_depth=state.get("nesting_depth", 0),
                    workflow_chain=state.get("workflow_chain", []),
                    parent_execution_id=state.get("parent_execution_id"),
                    timeout_budget_ms=state.get("timeout_budget_ms"),
                    skills=state.get("skills", []),
                    current_input=[],
                )
            except Exception as e:
                return await _fail_node(
                    state, log, execution_id, node_id,
                    f"Context initialization failed: {e}",
                )

            # 2. Resolve input items (once — shared by before_node and handler).
            context.current_input = context.get_input_for_node(node_id, edges_for_closure)
            input_data = _first_item_json(context.current_input)

            # 3. Orchestrator `before_node` hook (FULL supervision only).
            if _should_call_full_hook(orchestrator, supervision_level):
                decision = await _safe_hook(
                    orchestrator.before_node, _ORCHESTRATOR_HOOK_TIMEOUT_S,
                    execution_id, node_id, node_type, state, input_data=input_data,
                )
                if isinstance(decision, AbortDecision):
                    state["status"] = "failed"
                    state["error"] = decision.reason
                    return state
                if isinstance(decision, PauseDecision):
                    state["status"] = "paused"
                    return state

            # 4. Resolve pre-analysed expressions in the config.
            resolved_config = context.resolve_expressions(node_config, expr_paths)

            # 5. Merge any externally-injected input for this node.
            injected_key = f"_input_{node_id}"
            if injected_key in state["node_outputs"]:
                injected = state["node_outputs"][injected_key]
                if isinstance(injected, dict):
                    input_data.update(injected)
                elif isinstance(injected, list) and injected:
                    input_data.update(_first_item_json(injected))

            # 6. Dispatch to handler.
            if not registry.has_handler(node_type):
                return await _fail_node(
                    state, log, execution_id, node_id,
                    f"Unknown node type: {node_type}",
                )
            handler = registry.get_handler(node_type)

            await log.log_node_start(
                execution_id=execution_id, node_id=node_id, node_type=node_type,
                node_name=node_id, input_data={"items": input_data}, config=node_config,
            )

            start = asyncio.get_event_loop().time()
            try:
                result = await asyncio.wait_for(
                    handler.execute(input_data, resolved_config, context),
                    timeout=timeout,
                )
            except Exception as e:
                duration_ms = int((asyncio.get_event_loop().time() - start) * 1000)
                return await _fail_node(
                    state, log, execution_id, node_id,
                    f"Node {node_id} error: {e}", duration_ms=duration_ms, exc_info=True,
                )
            duration_ms = int((asyncio.get_event_loop().time() - start) * 1000)

            serialized_items = [it.model_dump(by_alias=True) for it in result.items]

            # 7. Sync mutable context state back. node_outputs / credentials are
            #    shared by reference (Pydantic passes dicts through unchanged);
            #    variables / loop_stats we copy to isolate handler-local changes.
            state["variables"] = dict(context.variables)
            state["loop_stats"] = dict(context.loop_stats)
            state["node_outputs"][node_id] = serialized_items
            state["node_outputs"][f"_handle_{node_id}"] = result.output_handle

            await log.log_node_complete(
                execution_id=execution_id, node_id=node_id,
                success=result.success,
                output_data={"items": serialized_items},
                error_message=result.error or "",
                duration_ms=duration_ms,
                warnings=[w.model_dump(by_alias=True) for w in context.warnings],
            )

            # 8. Loop iteration bookkeeping.
            if node_type in LOOP_NODE_TYPES:
                state["loop_stats"][node_id] = state["loop_stats"].get(node_id, 0) + 1

            # 9. Failure path — optional on_error orchestrator hook.
            if not result.success:
                await _handle_node_failure(
                    state, orchestrator, supervision_level, execution_id,
                    node_id, node_type, result.error,
                )
                return state

            # 10. Feed the just-produced items into any downstream loop's
            #     accumulator — but ONLY if this edge is a body-return edge
            #     (the source is downstream of the loop in forward reachability).
            for edge in outgoing_edges:
                target = edge.get("target")
                target_type = get_node_type(self._node_map.get(target, {}))
                if target_type in LOOP_NODE_TYPES and node_id in loop_body_sources.get(target, set()):
                    acc_key = f"_accumulated_{target}"
                    state["variables"].setdefault(acc_key, []).extend(serialized_items)

            # 11. Orchestrator `after_node` hook (FULL supervision only).
            if _should_call_full_hook(orchestrator, supervision_level):
                post = await _safe_hook(
                    orchestrator.after_node, _ORCHESTRATOR_HOOK_TIMEOUT_S,
                    execution_id, node_id,
                    {"items": serialized_items, "output_handle": result.output_handle},
                    state,
                )
                if isinstance(post, AbortDecision):
                    state["status"] = "failed"
                    state["error"] = post.reason
                elif isinstance(post, PauseDecision):
                    state["status"] = "paused"

            return state

        return node_function


# ---------------------------------------------------------------------------
# Module-level helpers. Keep these stateless so they are trivially testable
# and don't capture the compiler instance unnecessarily.
# ---------------------------------------------------------------------------

def _find_expression_paths(config: Any, current: list | None = None) -> list[list]:
    """Walk a config tree and return the path to every string containing `{{ }}`."""
    if current is None:
        current = []
    paths: list[list] = []
    if isinstance(config, dict):
        for k, v in config.items():
            paths.extend(_find_expression_paths(v, current + [k]))
    elif isinstance(config, list):
        for i, v in enumerate(config):
            paths.extend(_find_expression_paths(v, current + [i]))
    elif isinstance(config, str) and "{{" in config and "}}" in config:
        paths.append(current)
    return paths


def _compute_loop_body_sources(
    nodes: list[dict], edges: list[dict],
) -> dict[str, set[str]]:
    """
    For each loop node, return the set of source nodes whose edge into the
    loop constitutes a body-return edge (i.e. the source is reachable from
    the loop via forward traversal).

    Example:
        start → loop → body1 → body2 → loop
    Here body2 → loop is a body-return edge; start → loop is NOT.
    """
    outgoing: dict[str, list[str]] = defaultdict(list)
    for e in edges:
        s, t = e.get("source"), e.get("target")
        if s and t:
            outgoing[s].append(t)

    loop_ids = {n["id"] for n in nodes if get_node_type(n) in LOOP_NODE_TYPES}
    result: dict[str, set[str]] = {}
    for loop_id in loop_ids:
        # BFS forward from loop_id; collect every node reachable (excluding loop_id
        # itself unless it revisits). The "body sources" are the reachable set;
        # the initial-feed sources are outside it.
        reachable: set[str] = set()
        queue = list(outgoing.get(loop_id, []))
        while queue:
            n = queue.pop()
            if n in reachable:
                continue
            reachable.add(n)
            queue.extend(outgoing.get(n, []))
        result[loop_id] = reachable
    return result


def _first_item_json(items: list) -> dict:
    """Return the first item's `.json` payload, or empty dict."""
    if not items:
        return {}
    first = items[0]
    if not isinstance(first, dict):
        return {}
    val = first.get("json", first)
    return val if isinstance(val, dict) else {}


def _should_call_full_hook(orchestrator: Any, supervision_level: Any) -> bool:
    if not orchestrator:
        return False
    # Accept both enum and stringly-typed supervision levels.
    from orchestrator.interface import SupervisionLevel
    return supervision_level not in (
        SupervisionLevel.ERROR_ONLY, SupervisionLevel.NONE, "error_only", "none",
    )


def _should_call_error_hook(orchestrator: Any, supervision_level: Any) -> bool:
    if not orchestrator:
        return False
    from orchestrator.interface import SupervisionLevel
    return supervision_level not in (SupervisionLevel.NONE, "none")


async def _safe_hook(fn, timeout, *args, **kwargs):
    """Invoke an orchestrator hook with timeout and exception isolation."""
    try:
        return await asyncio.wait_for(fn(*args, **kwargs), timeout=timeout)
    except asyncio.TimeoutError:
        logger.warning(f"Orchestrator hook {fn.__name__} timed out")
    except Exception as e:
        logger.error(f"Orchestrator hook {fn.__name__} failed: {e}")
    return None


async def _handle_node_failure(
    state: WorkflowState,
    orchestrator: Any,
    supervision_level: Any,
    execution_id: UUID,
    node_id: str,
    node_type: str,
    error_message: str | None,
) -> None:
    """Route a handler's logical failure through on_error or fail directly."""
    from orchestrator.interface import AbortDecision

    if _should_call_error_hook(orchestrator, supervision_level):
        decision = await _safe_hook(
            orchestrator.on_error, _ORCHESTRATOR_HOOK_TIMEOUT_S,
            execution_id, node_id, node_type, error_message, state,
        )
        if isinstance(decision, AbortDecision) or decision is None:
            state["error"] = error_message
            state["status"] = "failed"
    else:
        state["error"] = error_message
        state["status"] = "failed"


async def _fail_node(
    state: WorkflowState,
    log,
    execution_id: UUID,
    node_id: str,
    error_message: str,
    duration_ms: int = 0,
    exc_info: bool = False,
) -> WorkflowState:
    """Terminal node failure — logs completion, mutates state, returns state."""
    state["error"] = error_message
    state["status"] = "failed"
    if exc_info:
        logger.exception(f"Node execution failed: {node_id}")
    else:
        logger.error(error_message)
    try:
        await log.log_node_complete(
            execution_id=execution_id, node_id=node_id,
            success=False, output_data={},
            error_message=error_message, duration_ms=duration_ms,
            status="failed",
        )
    except Exception as log_err:
        logger.error(f"Failed to log node failure: {log_err}")
    return state
