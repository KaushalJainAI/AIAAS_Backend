# Deep Dive: Inside Node Execution

This document details the precise mechanics of what happens during the exact moment a single node runs within the Execution Engine's LangGraph loop. 

To see how we get to this point from an API call, see [DRY_RUN.md](./DRY_RUN.md).

## The Stage

The `ExecutionEngine` is running. The underlying LangGraph engine determines that it is time to execute Node ID `node_code_1`. LangGraph invokes the `node_function` wrapper created during compilation (`compiler.py:_create_node_function`).

The global state entering this function is the **WorkflowState** (a `TypedDict` containing previous outputs, execution UUID, and the user ID).

---

## Step 1: Context Initialization  (`compiler.py`)

Before the node logic is touched, the system builds an isolated environment.

1.  **The `logger_instance`:** An `ExecutionLogger` is created immediately to ensure all subsequent crashes or successes are recorded to the database.
2.  **The `ExecutionContext`:** An instance of `ExecutionContext` is instantiated. This acts as the isolated "sandbox" for the node, bundling variables, credentials, and state.
    > 📚 See [STATE_MANAGEMENT.md](./STATE_MANAGEMENT.md) for the complete schema and explanation of `ExecutionContext` and the global `WorkflowState`.

---

## Step 2: Input Resolution (`compiler.py`)

Nodes rarely operate in a vacuum; they need data from upstream nodes. 

1.  **The Call:** The wrapper calls `context.get_input_for_node(node_id, self.edges)`.
2.  **The Trace:** The context looks at the incoming edges for this specific node.
3.  **The Extraction:** It fishes the output data of the upstream node from the global `state['node_outputs']`.
4.  **The Consolidation:** The extracted data is bundled into a list of `NodeItem` objects (`json` and `binary` payloads). This consolidated input becomes the `input_data` for the upcoming execution.

---

## Step 3: Expression Evaluation (`compiler.py`)

A user might configure a Telegram node text field like this: `Hello {{ $node["GitHub Trigger"].json.triggered_at }}`. The node handler should not deal with `{{ }}` strings; it just needs the final interpolated text.

1.  **Pre-computation:** During compilation, the compiler pre-cached the location of all `{{ }}` strings in the node's JSON config.
2.  **Resolution:** The wrapper calls `context.resolve_expressions(node_config, expr_paths)`. 
3.  **Replacement:** The context evaluates the variables using `_resolve_string_expression` against the data state:
    *   **Node References**: `{{ $node["Node Name"].json.field }}` or `{{ $node['Node Name'].json.field }}` looks up the output of an executed node by its label, digs into the `json` wrapper (standard workflow item format), and extracts `field`.
    *   **Current Input**: `{{ $json.field }}` or `{{ $input.field }}` references the data passed directly into the current node.
    *   **Global Event Data**: `{{ event.body.field }}` references the initial trigger payload (commonly from webhooks).
    *   **Variables**: `{{ $vars.user_id }}` looks up `state['variables']['user_id']`.
4.  **Result:** `resolved_config` now contains `Hello 2026-02-21T11:42:39.257862`.

---

## Step 4: Execution Sandbox (`nodes/handlers/...`)

The wrapper looks up the node type in the global registry (e.g., `CodeNodeHandler`) and calls its asynchronous `execute()` method.

### Example: Running Custom Code (`safe_execution.py`)
If this is a **Code Node**:
1.  **Isolation:** The handler pulls the python string from `resolved_config`.
2.  **Global Restriction:** It creates a highly restricted `globals()` dictionary. Built-ins like `open()`, `eval()`, or `__import__` are removed or blocked to prevent malicious shell execution.
3.  **Execution:** The code runs in an isolated `exec()` call.
4.  **Timeout:** The execution is wrapped in an `asyncio.wait_for(..., timeout=timeout_budget)`. If the user writes a `while True:` loop, it will violently abort upon timeout.

### Standardized Output
Regardless of whether it is an `HttpRequestNode`, `IfNode`, or `CodeNode`, the handler *must* return a `NodeExecutionResult` object (`handlers/base.py`).

```python
# The returned object
NodeExecutionResult(
    success=True,
    items=[
        NodeItem(json={"batch_id": 2500})
    ],
    output_handle="default" 
)
```
*Note: The `output_handle` (e.g., "true", "false", "default", "loop") is critical, as it allows LangGraph to intelligently route the edge conditionally.*

---

## Step 5: Post-Execution Teardown (`compiler.py`)

The handler returns control to the `node_function` wrapper.

1.  **State Update & Loop Accumulation:** The wrapper serializes the `result.items` into standard dictionaries and injects them into the global graph state:
    *   `state['node_outputs']["node_code_1"] = serialized_items`
    *   `state['node_outputs']["_handle_node_code_1"] = "default"`
    *   If the target of this node's outgoing edge is a loop type (`loop`, `split_in_batches`), the results are additionally appended to `state['variables']["_accumulated_{target_id}"]` to support deferred batch processing.
2.  **Telemetry:** `logger_instance.log_node_complete(...)` is fired, writing the final JSON payload and execution duration (ms) to the database.
3.  **Crash Safety:** The entire node execution is wrapped in a fail-safe `try/except`. If the node catastrophically crashes (e.g., unhandled exception in custom code), the compile wrapper catches it, manually creates a `failed` status entry via `log_node_complete`, and ensures the UI and Orchestrator are notified instead of hanging indefinitely.
4.  **Return:** The updated `WorkflowState` dictionary is returned to the LangGraph engine.

## Step 6: Edge Routing
LangGraph looks at the returned state. It checks the conditional route evaluation function dynamically built by the compiler, reads the recorded output handle, and transitions perfectly to the next node.
