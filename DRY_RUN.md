# Workflow Pipeline: Detailed Dry Run Simulation

This document provides a comprehensive, step-by-step simulation of a workflow execution. It explicitly details the **Data Flow**, **Security Checks**, **Component Interactions**, and **Database State Changes** at every stage of the pipeline.

**Objective**: To transparently inspect how the backend processes a request from API to Database to Completion, using a concrete "User Verification" example.

---

## 0. The Workflow Definition (Database State)

Before execution begins, the workflow exists as a row in the `Backend.orchestrator.models.Workflow` table. 

**Full JSON Representation**:
This is the exact structure stored in the database.

```json
{
  "id": 1,
  "name": "User Verification Flow",
  "slug": "user-verification-flow",
  "description": "Validates user ID and fetches details if eligible.",
  "user_id": 1,
  "status": "active",
  "execution_count": 42,
  "workflow_settings": {
    "timeout_seconds": 60,
    "max_retries": 1,
    "error_policy": "fail_fast"
  },
  "viewport": {
    "x": 100,
    "y": 200,
    "zoom": 1.2
  },
  "nodes": [
    {
      "id": "node_trigger_1",
      "type": "manual_trigger",
      "position": { "x": 0, "y": 0 },
      "data": {
        "label": "Start Request"
      }
    },
    {
      "id": "node_code_1",
      "type": "code",
      "position": { "x": 200, "y": 0 },
      "data": {
        "label": "Calculate Batch ID",
        "code": "return {'batch_id': input['user_id'] + 1000, 'timestamp': '2023-01-01'}"
      }
    },
    {
      "id": "node_if_1",
      "type": "if_condition",
      "position": { "x": 400, "y": 0 },
      "data": {
        "label": "Is High Value?",
        "expression": "{{ $input.batch_id }} > 2000"
      }
    },
    {
      "id": "node_http_1",
      "type": "http_request",
      "position": { "x": 600, "y": -100 },
      "data": {
        "label": "Fetch User Profile",
        "method": "GET",
        "url": "https://api.internal.system/users/{{ $input.batch_id }}",
        "credential_id": "cred_internal_api_001"
      }
    },
    {
      "id": "node_notify_1",
      "type": "notification",
      "position": { "x": 600, "y": 100 },
      "data": {
        "label": "Log Skip",
        "channel": "slack",
        "message": "Skipped low value batch {{ $input.batch_id }}"
      }
    }
  ],
  "edges": [
    {
      "id": "edge_1",
      "source": "node_trigger_1",
      "target": "node_code_1",
      "type": "default"
    },
    {
      "id": "edge_2",
      "source": "node_code_1",
      "target": "node_if_1",
      "type": "default"
    },
    {
      "id": "edge_3",
      "source": "node_if_1",
      "target": "node_http_1",
      "sourceHandle": "true",
      "type": "condition"
    },
    {
      "id": "edge_4",
      "source": "node_if_1",
      "target": "node_notify_1",
      "sourceHandle": "false",
      "type": "condition"
    }
  ]
}
```

---

## 1. Phase 1: API Request & Entry

**Scenario**: A user manually triggers this workflow via the Frontend or API.

**Endpoint**: `POST /api/orchestrator/workflows/1/execute/`

### 1.1 Request Payload
```json
{
  "input_data": {
    "user_id": 1500,
    "source": "dashboard"
  },
  "async": true
}
```

### 1.2 View Layer Logic (`Backend/orchestrator/views.py`)
1.  **Auth Check**: `request.user` is authenticated.
2.  **Permission**: `Workflow.objects.get(id=1, user=request.user)` validates ownership.
3.  **Credential Retrieval**:
    *   Calls `get_user_credentials(user_id=1)`.
    *   **Decrypts**: `{ "cred_internal_api_001": { "api_key": "sk-secure-key..." } }`.
    *   *Note*: These are kept in memory only; never logged to DB.
4.  **Async Launch**: Pass payload to `WorkflowOrchestrator.start()`.

---

## 2. Phase 2: Compilation & Graph Build (`Backend/compiler/compiler.py`)

The raw JSON is compiled **directly** into an executable LangGraph StateGraph (Single-Pass Architecture).

### 2.1 Input
The `nodes`, `edges`, and `user_credentials` are passed to `WorkflowCompiler`.

### 2.2 Validation Steps (Fail-Fast)
1.  **DAG Check**: `validate_dag` confirms no cycles exist.
2.  **Creds Check**: Verifies `cred_internal_api_001` exists in the user's keychain.
3.  **Type Check**: `validate_type_compatibility` passes.

If any check fails, a `WorkflowCompilationError` is raised immediately, halting execution.

### 2.3 Output: CompiledStateGraph
The compiler constructs the graph in-memory:
1.  **Nodes**: Each workflow node is wrapped in a `node_function` with Orchestrator hooks.
2.  **Edges**: Routing logic is baked into LangGraph structure (including conditional branches).
3.  **Entry Point**: `node_trigger_1` is identified and set as the start.

*No intermediate JSON execution plan is generated.*

---

## 3. Phase 3: Runtime Initialization

The `WorkflowOrchestrator` initializes the `ExecutionLog` entry before invoking the graph.

### 3.1 DB INSERTS (ExecutionLog)
```python
ExecutionLog.objects.create(
    execution_id="exc-885522-uuid4",
    workflow_id=1,
    user_id=1,
    status="running",
    trigger_type="manual",
    input_data={ "user_id": 1500, "source": "dashboard" },  # From API Payload
    started_at="2026-01-27T10:00:00Z",
    nodes_executed=0
)
```

---

## 4. Phase 4: Validated Execution Flow (Step-by-Step)

The engine iterates through the graph. We follow the path determined by the data.

### Node 1: Manual Trigger (`node_trigger_1`)

1.  **Input Context**: Global `{ "user_id": 1500, "source": "dashboard" }`
2.  **Logic**: Pass-through.
3.  **Output**: `{ "user_id": 1500, "source": "dashboard" }`
4.  **Logging**:
    *   `INSERT` into `NodeExecutionLog` (status='running').
    *   `UPDATE` `NodeExecutionLog` (status='completed', output_data={...}).

### Node 2: Code Execution (`node_code_1`)

1.  **Input Context**: `{ "user_id": 1500, "source": "dashboard" }` (From trigger)
2.  **Code Logic**:
    ```python
    input['user_id'] + 1000  # 1500 + 1000 = 2500
    ```
3.  **Output**:
    ```json
    {
      "batch_id": 2500,
      "timestamp": "2023-01-01"
    }
    ```
4.  **Logging**: `NodeExecutionLog` updated with result.

### Node 3: If Condition (`node_if_1`)

1.  **Input Context**: `{ "batch_id": 2500, "timestamp": "..." }`
2.  **Expression Eval**:
    *   `{{ $input.batch_id }} > 2000`
    *   `2500 > 2000` is **TRUE**.
3.  **Routing Decision**:
    *   The engine checks outgoing edges from `node_if_1`.
    *   It selects Edge `edge_3` because `sourceHandle="true"`.
    *   It *ignores* Edge `edge_4` (`sourceHandle="false"`).
4.  **Output Data**: `{ "result": true }`
5.  **Logging**: `NodeExecutionLog` saves `output_handle="true"`.

### Node 4: HTTP Request (`node_http_1`)

1.  **Input Context**: `{ "batch_id": 2500 }` (Resolved from `node_code_1` output via context scope).
2.  **Variable Substitution**:
    *   URL Template: `https://api.internal/users/{{ $input.batch_id }}`
    *   Resolved URL: `https://api.internal/users/2500`
3.  **Credential Usage**:
    *   Injects header: `Authorization: Bearer sk-secure-key...` (Decrypted in Phase 1).
4.  **Execution**: Performs `GET` request.
5.  **Response**:
    ```json
    {
      "id": 2500,
      "status": "active",
      "premium": true
    }
    ```
6.  **Logging**: `NodeExecutionLog` saves the response body.

### (Skipped) Node 5: Notification (`node_notify_1`)
*   This node is **NEVER** reached because the "False" path was not taken.
*   No database entry is created for this node.

---

## 5. Phase 5: Completion & Summary

The execution reaches a leaf node (`node_http_1`). The loop terminates.

### 5.1 Final DB Update (`Backend.logs.models.ExecutionLog`)

The master record is updated to reflect success.

```python
ExecutionLog.objects.filter(execution_id="exc-885522-uuid4").update(
    status="completed",
    completed_at="2026-01-27T10:00:01Z",
    duration_ms=450,
    nodes_executed=4,  # Trigger, Code, If, Http
    output_data={      # The output of the final leaf node
      "id": 2500,
      "status": "active",
      "premium": true
    }
)
```

### 5.2 Workflow Statistics Update
```python
Workflow.objects.filter(id=1).update(
    execution_count=43,        # Increment
    successful_executions=40,  # Increment
    last_executed_at="2026-01-27T10:00:01Z"
)
```

### 5.3 Full Trace Table

| Sequence | Node Type | Node ID | Status | Output Preview |
| :--- | :--- | :--- | :--- | :--- |
| 1 | `manual_trigger` | `node_trigger_1` | `completed` | `{"user_id": 1500}` |
| 2 | `code` | `node_code_1` | `completed` | `{"batch_id": 2500}` |
| 3 | `if_condition` | `node_if_1` | `completed` | `{"result": true}` |
| 4 | `http_request` | `node_http_1` | `completed` | `{"id": 2500, "premium": true}` |
| - | `notification` | `node_notify_1` | - | *(Not Executed)* |

---

## 6. Phase 6: Nested Workflow Recursion

This section details how a **Parent Workflow** calls the "User Verification Flow" (ID 1) defined above.

**Scenario**: A "Batch Processor" workflow (Parent) iterates through inputs and calls the child workflow for each item.

### 6.1 Parent Workflow Node Definition
The parent workflow contains a `subworkflow` node configured to call Workflow ID 1.

```json
{
  "id": "node_call_child",
  "type": "subworkflow",
  "data": {
    "workflow_id": 1,
    "input_mapping": {
      "user_id": "{{ $input.current_user_id }}"
    },
    "output_mapping": {
      "verification_result": "{{ $output.data.status }}"
    }
  }
}
```

### 6.2 Execution Logic (`Backend/nodes/handlers/subworkflow_node.py`)

1.  **Recursion Check**:
    *   Backend reads `context.nesting_depth` (e.g., 0).
    *   Validates `nesting_depth < max_nesting_depth` (Default: 3).
    *   **Pass**: Depth is 0, allowed.

2.  **Circular Dependency Check**:
    *   Checks if Workflow ID 1 is already in the execution stack.
    *   **Pass**: No circle.

3.  **Input Transformation**:
    *   Evaluates `{{ $input.current_user_id }}` from parent context --> `1500`.
    *   Constructs Child Input: `{ "user_id": 1500 }`.

4.  **Orchestrator Recursion**:
    *   Calls `WorkflowOrchestrator.execute_subworkflow()`.
    *   **Crucial Step**: Creates a **NEW** `ExecutionLog` entry for the child.

### 6.3 Database State (Child Execution)

```python
ExecutionLog.objects.create(
    execution_id="exc-child-999",
    workflow_id=1,
    parent_execution_id="exc-parent-888",  # Linked to Parent
    nesting_depth=1,                       # Incremented
    is_subworkflow_execution=True,
    status="running",
    input_data={ "user_id": 1500 }
)
```

### 6.4 Child Completion & Return
1.  The child workflow runs physically (phases 1-5 above).
2.  It completes with output: `{ "id": 2500, "status": "active", "premium": true }`.
3.  **Output Mapping (Back to Parent)**:
    *   The `subworkflow` node applies the mapping: `verification_result` = `data.status` ("active").
    *   Parent Node Output: `{ "verification_result": "active" }`.

### 6.5 Parent Log Update
The parent's node log references the child execution.

```python
NodeExecutionLog.objects.create(
    execution_id="exc-parent-888",
    node_id="node_call_child",
    child_execution_id="exc-child-999",  # Database Link
    status="completed",
    output_data={ "verification_result": "active" }
)
```

---

## 7. Phase 7: MCP Tool Execution (NEW)

This section details how an **MCP Tool Node** interacts with an external MCP Server (e.g., filesystem).

**Scenario**: A workflow step requires reading a file from the server's disk using the `filesystem` MCP server.

### 7.1 Node Definition

```json
{
  "id": "node_mcp_read",
  "type": "mcp_tool",
  "data": {
    "server_id": 101,
    "tool_name": "read_file",
    "arguments": {
      "path": "/var/logs/app.log"
    }
  }
}
```

### 7.2 Connection Establishment (`Backend/mcp_integration/client.py`)

1.  **Server Config Lookup**:
    *   Fetches `MCPServer(id=101)` from DB.
    *   Type: `stdio`, Command: `npx`, Args: `['@modelcontextprotocol/server-filesystem', '/var/logs']`.

2.  **Process Spawn**:
    *   Backend spawns the node process via `subprocess.Popen` (Stdio).
    *   Protocol handshake (Initialize) occurs.

3.  **Tool List Verification**:
    *   Backend implicitly trusts the tool name `read_file` exists (or validates against cached list).

### 7.3 Tool Execution

1.  **Call Tool**:
    *   Backend sends JSON-RPC request: `{ "method": "tools/call", "params": { "name": "read_file", "arguments": { "path": "/var/logs/app.log" } } }`.
2.  **Server Action**:
    *   External process reads the file from disk.
    *   Returns content.
3.  **Response Handling**:
    *   Backend receives: `{ "content": [{ "type": "text", "text": "Error: Connection timed out..." }] }`.
    *   Parsed into python dict: `['Error: Connection timed out...']`.

### 7.4 Logging & Teardown

1.  **Logging**: `NodeExecutionLog` saves the file content as output.
2.  **Teardown**: The stdio process is terminated (ephemeral connection).

### 7.5 Database State

```python
NodeExecutionLog.objects.create(
    execution_id="exc-mcp-test-1",
    node_id="node_mcp_read",
    status="completed",
    output_data=["Error: Connection timed out..."]
)
```

---

## 8. Phase 8: LangChain Tool Execution (NEW)

This section details how a **LangChain Tool Node** interacts with standard LangChain tools (e.g., Wikipedia).

**Scenario**: A workflow step requires fetching a summary from Wikipedia.

### 8.1 Node Definition

```json
{
  "id": "node_lc_wiki",
  "type": "langchain_tool",
  "data": {
    "tool_name": "wikipedia",
    "query": "LangChain",
    "config": {
      "tool_name": "wikipedia",
      "query": "LangChain"
    }
  }
}
```

### 8.2 Execution Logic (`Backend/nodes/handlers/langchain_nodes.py`)

1.  **Tool Lookup**:
    *   Backend checks `TOOLS` registry for "wikipedia".
    *   Found `WikipediaQueryRun`.

2.  **Execution**:
    *   Calls `tool.run("LangChain")` (in thread pool if sync).
    *   LangChain wrapper calls Wikipedia API.

3.  **Output**:
    *   Returns: `{ "result": "LangChain is a framework designed to simplify the creation of applications..." }`.

### 8.3 Database State

```python
NodeExecutionLog.objects.create(
    execution_id="exc-lc-test-1",
    node_id="node_lc_wiki",
    status="completed",
    output_data={ "result": "LangChain is a framework..." }
)
```



## 9. Phase 9: Internal Graph Logic & Looping (Reference)

This section explains the internal logic baked into the **CompiledStateGraph** created in Phase 2.

**Scenario**: A "Retry Loop" where we try a task up to 3 times.
**Graph**: `Trigger` -> `Loop Node` -> `Task (Code)` -> `Loop Node`

### 9.1 LangGraph State Schema
The graph maintains a `WorkflowState` (TypedDict) internally:
*   `node_outputs`: Stores results of every node.
*   `loop_stats`: Dictionary `{ "node_loop_1": int }` tracking iterations.
*   `status`: Controls flow (running/paused/failed).

### 9.2 Conditional Edge Logic
For the Loop Node, the compiler generates a dynamic routing function:
```python
def route_conditional(state):
    # Reads the handle returned by the node execution
    handle = state['node_outputs'].get("_handle_node_loop_1")
    if handle == 'loop': return "node_code_task"
    if handle == 'done': return END
    return END
```

### 9.3 Loop Runtime Execution (`Backend/nodes/handlers/logic_nodes.py`)
**Node**: `node_loop_1` (Max Loop Count: 3)

#### **Iteration 1**:
1.  **State**: `loop_stats['node_loop_1']` is undefined (0).
2.  **Check**: `0 < 3` -> **True**.
3.  **State Update**: `loop_stats` -> `1`. Returns `"loop"`.
4.  **Route**: To `node_code_task`.

#### **Iteration 2 & 3**:
Same logic, incrementing count.

#### **Iteration 4 (Termination)**:
1.  **Check**: `3 < 3` -> **False**.
2.  **Result**: Returns `"done"`.
3.  **Route**: To `END`.

---

## 10. Phase 10: Orchestrator Governance & Logic Interception

In addition to physical execution, the **Orchestrator** acts as a supervisory layer. It injects hooks *before* and *after* each node to enforce policy.

### 10.1 The Supervisor Loop (`Backend/executor/orchestrator.py`)
Because the orchestrator manages the entire graph, it makes decisions at every step.

#### **Hook 1: `before_node()`**
Before `node_code_1` executes:
1.  **Pause Check**: Is the execution paused?
    *   If `state==PAUSED`, execution halts here until resumed.
2.  **Cancellation Check**: Is `state==CANCELLED`?
    *   If true, returns `AbortDecision`.
3.  **Progress Notification**:
    *   Updates in-memory state: `handle.current_node = "node_code_1"`.
    *   Emits websocket event: `execution_progress { node_id: "node_code_1", progress: 20% }`.

#### **Hook 2: `after_node()`**
After `node_code_1` finishes:
1.  **Loop Counting**: logic checks if `output_handle == 'loop'`.
    *   Increments `handle.loop_counters['node_id']`.
2.  **System Safety Check**:
    *   Enforces `SYSTEM_MAX_LOOPS = 1000`.
    *   If exceeded: Returns `AbortDecision("System safety limit exceeded")`.

#### **Hook 3: `on_error()`**
If `node_code_1` throws an exception:
1.  **Logging**: Logs "Error in node node_code_1: Division by zero".
2.  **Policy Decision**:
    *   Currently defaults to `AbortDecision`.
    *   *(Future)*: Could check "Continue on Error" flag and return `ContinueDecision`.

This multi-layer architecture ensures that the system remains responsive (pausable/cancellable) and safe (infinite loop protection) regardless of the underlying graph logic.
