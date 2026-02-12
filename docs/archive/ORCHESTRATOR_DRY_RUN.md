# King Agent Orchestrator: Implementation Dry Run

This document provides a step-by-step simulation of the **KingOrchestrator** (`executor/king.py`) managing a workflow execution. It is based on the **actual implementation** and highlights both working features and current limitations (stubs).

---

## Architecture Recap

```
┌─────────────────────────────────────────────────────────────────┐
│                     👑 KING AGENT (king.py)                      │
│  Intent Reception │ Lifecycle Control │ HITL │ Supervision      │
└──────────────────────────────┬──────────────────────────────────┘
                               │ Delegates To
                               ▼
┌─────────────────────────────────────────────────────────────────┐
│                     ⚙️ EXECUTION ENGINE (engine.py)              │
│  Compile Workflow │ Run LangGraph │ Report State                 │
└─────────────────────────────────────────────────────────────────┘
```

---

## Scenario: Execute & Control a Workflow

**Goal**: User starts a 3-node workflow, pauses it mid-execution, then resumes.

**Workflow JSON**:
```json
{
  "id": 42,
  "name": "Test Flow",
  "nodes": [
    { "id": "node_trigger", "type": "manual_trigger" },
    { "id": "node_code", "type": "code", "data": { "code": "return {'x': 1}" } },
    { "id": "node_http", "type": "http_request", "data": { "url": "https://api.example.com" } }
  ],
  "edges": [
    { "source": "node_trigger", "target": "node_code" },
    { "source": "node_code", "target": "node_http" }
  ]
}
```

---

## Phase 1: Starting Execution

### 1.1 API Call
```python
# orchestrator/views.py (Simplified)
from executor.king import get_orchestrator

king = get_orchestrator()
handle = await king.start(
    workflow_json=workflow_json,
    user_id=1,
    input_data={"source": "dashboard"}
)
```

### 1.2 King's Actions (`king.py:start`)

| Line | Action | State Change |
|------|--------|--------------|
| 189  | Generate `execution_id` (UUID) | `uuid4()` -> `exc-abc-123` |
| 192  | Create `ExecutionHandle` | `state=PENDING` |
| 201  | Store in `_executions` dict | `{exc-abc-123: handle}` |
| 204  | Create `asyncio.Event` for pause | `_pause_events[exc-abc-123] = Event(set=True)` |
| 209  | Create async task for Engine | `asyncio.create_task(...)` |

**Outcome**: Returns `ExecutionHandle` immediately. Execution runs in background.

### 1.3 In-Memory State After Start

```python
king._executions = {
    UUID("exc-abc-123"): ExecutionHandle(
        execution_id=UUID("exc-abc-123"),
        workflow_id=42,
        user_id=1,
        state=ExecutionState.PENDING,  # Will change to RUNNING when task starts
        current_node=None,
        progress=0.0,
        started_at=datetime(2026, 2, 3, 12, 0, 0)
    )
}
king._pause_events = {
    UUID("exc-abc-123"): asyncio.Event()  # Is SET (not paused)
}
```

---

## Phase 2: Engine Executes the Graph

### 2.1 Engine Startup (`engine.py:run_workflow`)

The King's background task calls `_run_with_engine`, which:
1. Sets `handle.state = RUNNING`
2. Calls `engine.run_workflow(...)` with all parameters

**Engine Actions**:

| Line | Action | Detail |
|------|--------|--------|
| 65   | Compile workflow | `WorkflowCompiler(workflow_json)` |
| 69   | Build LangGraph | `compiler.compile(orchestrator=self.orchestrator)` |
| 79   | Create logger | `ExecutionLogger().start_execution(...)` |
| 117  | Invoke graph | `await graph.ainvoke(initial_state)` |

### 2.2 The LangGraph Loop

LangGraph executes nodes in topological order. For each node, the **compiled wrapper** calls back to the King's hooks.

---

## Phase 3: Supervision Hooks

### 3.1 Before Node (`king.py:before_node`)

**Trigger**: LangGraph is about to run `node_code`.

```python
decision = await orchestrator.before_node(
    execution_id=UUID("exc-abc-123"),
    node_id="node_code",
    context={"variables": {...}}
)
```

**King's Logic**:
```python
# Line 244-246: Check if handle exists
handle = self._executions.get(execution_id)
if not handle:
    return AbortDecision("Handle lost")

# Line 249-256: Check if paused
pause_event = self._pause_events.get(execution_id)
if pause_event and not pause_event.is_set():  # Event is CLEAR = paused
    handle.state = ExecutionState.PAUSED
    self._notify_state_change(handle)
    await pause_event.wait()  # ⏸️ BLOCK HERE UNTIL RESUMED
    handle.state = ExecutionState.RUNNING
    self._notify_state_change(handle)

# Line 258-259: Check if cancelled
if handle.state == ExecutionState.CANCELLED:
    return AbortDecision("Cancelled")

# Line 261-264: Update progress and continue
handle.current_node = node_id
self._notify_progress(execution_id, node_id, handle.progress)
return ContinueDecision()
```

**Outcome**: Returns `ContinueDecision()`. Node runs.

### 3.2 After Node (`king.py:after_node`)

**Trigger**: `node_code` completed successfully with output `{"x": 1}`.

```python
decision = await orchestrator.after_node(
    execution_id=UUID("exc-abc-123"),
    node_id="node_code",
    result={"x": 1, "output_handle": "success"},
    context={...}
)
```

**King's Logic**:
```python
# Line 270-274: Loop tracking (safety limit)
if result.get('output_handle') == 'loop':
    handle.loop_counters[node_id] += 1
    if handle.loop_counters[node_id] > 1000:
        return AbortDecision("Loop limit exceeded")

return ContinueDecision()
```

**Outcome**: Normal node, returns `ContinueDecision()`.

---

## Phase 4: Pause & Resume

### 4.1 User Requests Pause

```python
# API call (e.g., from frontend button)
await king.pause(execution_id=UUID("exc-abc-123"))
```

**King's Logic (`king.py:pause`)**:
```python
# Line 289-300
event = self._pause_events.get(execution_id)
if event:
    event.clear()  # ⬇️ CLEAR the event = paused
    handle = self._executions.get(execution_id)
    if handle:
        handle.state = ExecutionState.PAUSED
        self._notify_state_change(handle)  # Push to WebSocket
    return True
```

**What Happens**:
- `_pause_events[exc-abc-123]` is now **CLEARED**.
- Next time `before_node` is called, it will **await** on this event, blocking execution.

### 4.2 Execution is Blocked

LangGraph tries to run `node_http`. The compiled wrapper calls `before_node`.

```python
# Inside before_node (Line 250-256)
pause_event = self._pause_events.get(execution_id)
if pause_event and not pause_event.is_set():  # True! Event is cleared.
    handle.state = ExecutionState.PAUSED
    self._notify_state_change(handle)
    await pause_event.wait()  # ⏸️ BLOCKED HERE
    # ... resumes below after event.set()
```

**In-Memory State**:
```python
handle.state = ExecutionState.PAUSED
handle.current_node = "node_http"  # Paused BEFORE this node
```

### 4.3 User Requests Resume

```python
await king.resume(execution_id=UUID("exc-abc-123"))
```

**King's Logic (`king.py:resume`)**:
```python
# Line 302-311
event = self._pause_events.get(execution_id)
if event:
    event.set()  # ⬆️ SET the event = unblock
    handle.state = ExecutionState.RUNNING
    self._notify_state_change(handle)
    return True
```

**What Happens**:
- `event.set()` releases the `await pause_event.wait()` in `before_node`.
- Execution continues to run `node_http`.

---

## Phase 5: Error Handling

### 5.1 Node Crashes

Suppose `node_http` throws `ConnectionError("Timeout")`.

The compiled node wrapper catches this and calls:
```python
decision = await orchestrator.on_error(
    execution_id=UUID("exc-abc-123"),
    node_id="node_http",
    error="ConnectionError: Timeout",
    context={...}
)
```

### 5.2 King's Decision (`king.py:on_error`)

```python
# Line 278-284
async def on_error(self, execution_id, node_id, error, context):
    logger.error(f"Node {node_id} error: {error}")
    return AbortDecision(error)  # ❌ Current: Always abort
```

**Current Limitation**: The implementation always returns `AbortDecision`. 

> **Future Enhancement**: This is where HITL error recovery would go:
> ```python
> # Proposed future logic:
> if policy.ask_human_on_error:
>     response = await self.ask_human(execution_id, f"Error: {error}. Retry?", ["Yes", "No"])
>     if response == "Yes":
>         return RetryDecision()
> return AbortDecision(error)
> ```

---

## Phase 6: HITL (Human-in-the-Loop)

### 6.1 The `ask_human` Method

This is a **working feature**. It can be called from custom nodes or future enhancements.

**How it works (`king.py:ask_human`)**:

1. King creates an `HITLRequest` object.
2. Sets `handle.state = WAITING_HUMAN`.
3. Notifies frontend via callback (`_on_hitl_request`).
4. Creates an `asyncio.Queue` and **blocks** waiting for response.
5. When API calls `submit_human_response()`, the queue receives the answer.
6. King sets state back to `RUNNING` and returns the response.

**Example Flow**:
```python
# Inside a custom node or orchestrator logic
response = await king.ask_human(
    execution_id=exec_id,
    question="Approve deployment to production?",
    options=["Yes", "No", "Defer"]
)
if response == "Yes":
    # proceed
```

**State During Wait**:
```python
handle.state = ExecutionState.WAITING_HUMAN
handle.pending_hitl = HITLRequest(
    id="req-xyz",
    request_type=HITLRequestType.CLARIFICATION,
    message="Approve deployment to production?",
    options=["Yes", "No", "Defer"]
)
```

---

## Phase 7: Completion

### 7.1 Engine Returns Final State

After all nodes complete, `engine.run_workflow` returns `ExecutionState.COMPLETED`.

### 7.2 King Updates Handle (`king.py:_run_with_engine`)

```python
# Line 231-238
final_state = await self.engine.run_workflow(...)
handle.state = final_state  # COMPLETED
handle.completed_at = timezone.now()
if final_state == ExecutionState.COMPLETED:
    handle.progress = 100.0
self._notify_state_change(handle)
```

**Final In-Memory State**:
```python
ExecutionHandle(
    execution_id=UUID("exc-abc-123"),
    state=ExecutionState.COMPLETED,
    progress=100.0,
    completed_at=datetime(2026, 2, 3, 12, 0, 5)
)
```

---

## Implementation Status Summary

| Feature | Status | Location |
|---------|--------|----------|
| Start Execution | ✅ Working | `king.py:start` |
| Pause/Resume | ✅ Working | `king.py:pause/resume` + `before_node` hook |
| Stop/Cancel | ✅ Working | `king.py:stop` |
| Progress Tracking | ✅ Working | `before_node` updates `current_node` |
| Loop Safety | ✅ Working (Branch-Aware) | `after_node` uses `node_id:branch` key |
| HITL (ask_human) | ✅ Working + Timeout | `king.py:ask_human` with `asyncio.wait_for` |
| User Isolation | ✅ Fixed | `_check_execution_auth` on all methods |
| HITL Auth | ✅ Fixed | `submit_human_response` validates `user_id` |
| Memory Cleanup | ✅ Fixed | `_cleanup_execution` on complete/cancel |
| Safe Callbacks | ✅ Fixed | `_safe_callback` wraps all callbacks |
| Intent to Workflow | ⚠️ Stub | `king.py:create_workflow_from_intent` returns empty |
| Error Recovery HITL | ⚠️ Not Implemented | `on_error` always aborts |
| Backtracking | ❌ Not Implemented | Conceptual only |

---

## Security Fixes Applied

| Vulnerability | Fix |
|---------------|-----|
| No user isolation | All control methods now require `user_id` and call `_check_execution_auth` |
| HITL response unauthenticated | `submit_human_response` now takes `user_id` and validates against request owner |
| HITL blocks forever | `ask_human` uses `asyncio.wait_for(timeout=timeout_seconds)` |
| Memory leak | `_cleanup_execution` removes from `_tasks`, `_pause_events` on completion |
| Callback exceptions crash | `_safe_callback` wraps callbacks in try/except |
| Pause race condition | `before_node` checks cancel state after resume |
| Loop counter shared | Uses `node_id:branch` key for branch-aware counting |

---

## Known Issues & Future Work

1. **`create_workflow_from_intent` is a stub**: Returns empty workflow. Needs LLM integration.
2. **`on_error` always aborts**: No retry or HITL recovery yet.
3. **No backtracking**: Cannot rewind to a previous node and re-execute.
4. **Progress percentage not calculated**: `handle.progress` is always 0 until completion.
5. **No persistence**: State is in-memory; server restart loses executions (deferred to future).

These are documented for future implementation phases.
