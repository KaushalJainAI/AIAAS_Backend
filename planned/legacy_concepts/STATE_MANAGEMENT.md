# Deep Dive: State Management & Data Flow

This document details exactly how data is managed, preserved, and passed between nodes during a workflow execution in AIAAS.

## The Dual-State Architecture

AIAAS uses two distinct but interacting state objects during execution:
1.  **The LangGraph `WorkflowState`** (The Global Blueprint)
2.  **The `ExecutionContext`** (The Node's Sandbox)

---

## 1. The Global Blueprint (`WorkflowState`)

Because AIAAS compiles to LangGraph, it must adhere to LangGraph's architecture. The global state is incredibly simple. It is a `TypedDict` defined in `compiler.py`:

```python
class WorkflowState(TypedDict):
    execution_id: str
    user_id: int
    workflow_id: int
    current_node: str         # The node currently processing
    node_outputs: dict        # The master ledger of all outputs
    variables: dict           # Global workflow variables
    credentials: dict         # Decrypted keys
    error: Optional[str]
    status: str               # running | completed | failed | paused | cancelled
    nesting_depth: int        # For Subworkflow recursion
    workflow_chain: list[int] # Circular dependency prevention
    parent_execution_id: Optional[str]
    timeout_budget_ms: Optional[int]
    skills: list[dict]
```

### The Master Ledger (`node_outputs`)
The most important key is `node_outputs`. This is an append-only (mostly) JSON dictionary that stores the final results of every node that has finished executing. 

Example:
```json
"node_outputs": {
  "_input_global": { "trigger_data": "user_clicked_button" },
  "node_code_1": [{"json": {"batch_id": 2500}}],
  "_handle_node_code_1": "default",
  "node_http_1": [{"json": {"status": 200}}]
}
```
*Note the `_handle_<node_id>` convention. This is how LangGraph's conditional edges know which path to take after a node finishes.*

---

## 2. The Node's Sandbox (`ExecutionContext`)

When a single node executes, it does **not** receive the raw `WorkflowState`. It would be dangerous to let a custom Python Code node arbitrarily modify the global state routing variables.

Instead, the `node_function` wrapper instantiates an `ExecutionContext` (`compiler/schemas.py`).

### Schema Definition
```python
class ExecutionContext(BaseModel):
    execution_id: UUID
    user_id: int
    # ... (inherited fields from global state)
    
    warnings: list[CompileError]
    loop_stats: dict[str, int]
    executed_nodes: list[str]
    
    # Context-specific evaluation maps
    node_label_to_id: dict[str, str]
    current_input: list[dict]
```

### Why a separate Context?
1.  **Safety:** It acts as a strict getter/setter API. A node can call `context.set_variable('count', 1)`, but it cannot accidentally overwrite `context.execution_id`.
2.  **Resolvers:** It contains the complex regular expressions required to parse `{{ $node["HTTP"].json.data }}` and evaluate it against the historical `node_outputs`.
3.  **Traceability:** It tracks `warnings`. If a node encounters a soft error (like a missing JSON field), it appends to `context.warnings` rather than aborting the whole graph.

---

## 3. The `NodeItem` Standard (n8n Compatibility)

To support complex workflows (and mimic n8n's robust data piping), AIAAS enforces that every node outputs an array of `NodeItem` objects, *never* just a raw dictionary.

### The Problem
If `Node A` fetches 3 webhooks and outputs `[ {"id": 1}, {"id": 2}, {"id": 3} ]`, and `Node B` is an HTTP Request, how many requests should `Node B` make? It should make 3.

### The Solution: `NodeItem` Arrays 
All node outputs are standardized in `nodes/handlers/base.py`:
```python
class NodeItem(BaseModel):
    json_data: dict[str, Any] = Field(alias="json")
    binary: dict[str, Any] | None = None
    pairedItem: dict[str, int] | None = None 
```

When `Node B` receives its input, the `_process_items` helper function automatically loops over the array, firing the `Node B` logic once for every `NodeItem` received. This ensures structural integrity as data flows downstream.

### Traceability (`pairedItem`)
When `Node B` finishes processing Item #1 from `Node A`, it attaches `"pairedItem": {"item": 0}` to its output. This allows the visual graph UI to draw lines showing exactly which output corresponds to which input, even across massive batches.
