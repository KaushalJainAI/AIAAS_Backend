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

## 2. Phase 2: Compilation (`Backend/compiler/compiler.py`)

The raw JSON from Section 0 is compiled into an execution plan.

### 2.1 Input
The `nodes` and `edges` arrays from the Workflow JSON.

### 2.2 Validation Steps
1.  **DAG Check**: `validate_dag` confirms no cycles exist.
2.  **Creds Check**: Verifies `cred_internal_api_001` exists in the user's keychain.
3.  **Type Check**: `validate_type_compatibility` (if simplified) passes.

### 2.3 Output: WorkflowExecutionPlan
This JSON is built by `WorkflowCompiler.compile()`. It defines the precise order and dependencies.

```json
{
  "workflow_id": 1,
  "execution_order": ["node_trigger_1", "node_code_1", "node_if_1", "node_http_1", "node_notify_1"],
  "entry_points": ["node_trigger_1"],
  "nodes": {
    "node_trigger_1": {
      "node_id": "node_trigger_1",
      "type": "manual_trigger",
      "config": { "label": "Start Request" },
      "dependencies": []
    },
    "node_code_1": {
      "node_id": "node_code_1",
      "type": "code",
      "config": { "code": "return {'batch_id': input['user_id'] + 1000...}" },
      "dependencies": ["node_trigger_1"]
    },
    "node_if_1": {
      "node_id": "node_if_1",
      "type": "if_condition",
      "config": { "expression": "{{ $input.batch_id }} > 2000" },
      "dependencies": ["node_code_1"]
    },
    "node_http_1": {
      "node_id": "node_http_1",
      "type": "http_request",
      "config": { "url": "...", "method": "GET" },
      "dependencies": ["node_if_1"]
    },
    "node_notify_1": {
      "node_id": "node_notify_1",
      "type": "notification",
      "config": { "channel": "slack" },
      "dependencies": ["node_if_1"]
    }
  }
}
```

---

## 3. Phase 3: Runtime Initialization

The `WorkflowOrchestrator` initializes the `ExecutionLog` entry before starting the LangGraph runner.

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
