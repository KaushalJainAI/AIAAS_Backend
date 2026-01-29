# King Agent: Decision-Making Dry Run

This document simulates the "brain" of the system: the **KingOrchestrator**. 
Unlike the `DRY_RUN.md` (which covers the *mechanical* data flow), this document focuses on **Intent, Supervision, and Strategy**.

---

## Scenario: "The Rescue Mission"

**User Intent**: *"My deployment failed. Help me fix it."*
**Context**: User is looking at a dashboard showing a "502 Bad Gateway" on the Payment Service.

---

## 1. Intent Phase (The Brain)

**Input**: Natural Language + Context
```json
{
  "user_id": 1,
  "prompt": "My deployment failed. Help me fix it.",
  "context": {
    "current_page": "/dashboard/deployments",
    "selected_service": "payment-service",
    "last_error": "502 Bad Gateway"
  }
}
```

### 1.1 King's Decision: "What is the plan?"
The King uses `ai_generated.py` (The Planner) to translate intent into a concrete workflow.

**Thought Process**:
1.  *Identify Goal*: Fix "payment-service" deployment.
2.  *Analyze Context*: Error is "502". Likely need to check logs and restart.
3.  *Formulate Plan*:
    *   Step 1: Fetch recent logs.
    *   Step 2: Analyze for root cause.
    *   Step 3: Ask human if they want to restart.
    *   Step 4: Execute restart.

**Outcome**: Generates Ephemeral Workflow ID 900.

---

## 2. Dispatch Phase (The Command)

The King does not run code himself. He commands the **ExecutionEngine** (The Worker).

**King's Action**:
```python
KingOrchestrator.start(
    workflow_id=900, 
    user_id=1, 
    input={"service": "payment-service"}
)
```

**State Change**:
*   Execution `exc-rescue-1` created.
*   State: `PENDING` -> `RUNNING`.

---

## 3. Supervision Phase (The Watch)

The Worker Engine runs the nodes. The King watches every step via hooks: `before_node`, `after_node`, `on_error`.

### Step 3.1: Fetch Logs (Node A)
*   **Worker**: "I am about to run Node A."
*   **King (Hook)**: "Proceed." (Status: Active)
*   **Worker**: Runs Node A. Success. Output: `["Error: Out of Memory"]`
*   **King (Hook)**: "Good. Log progress."

### Step 3.2: Root Cause Analysis (Node B - AI Node)
*   **Worker**: Runs Node B (LLM).
*   **Output**: `"The service ran out of memory (OOM). Recommendation: Increase RAM or Restart."`

---

## 4. The Intervention Phase (HITL)

The workflow reaches a crucial decision point designed by the Planner: **Ask the Human**.

### Step 4.1: The Proposal
**Worker**: Hits `node_ask_human`.
**King (Decision)**:
1.  *Pause Execution*: The Engine cannot proceed without input.
2.  *Create Request*:
    ```python
    HITLRequest(
        type="approval",
        msg="Service OOM detected. Restart with 2x RAM?",
        options=["Yes", "No", "Just Restart"]
    )
    ```
3.  *Notify User*: Pushes WebSocket event.

**State Change**: `RUNNING` -> `WAITING_HUMAN`.

### Step 4.2: The Human Response
**User**: Clicks "Yes" (Restart with 2x RAM).
**API**: Calls `KingOrchestrator.submit_human_response("Yes")`.

**King (Action)**:
1.  *Receive Input*: "Yes".
2.  *Update Context*: Injects `ram_multiplier=2` into the workflow variables.
3.  *Resume Engine*: Signals the Worker to wake up.

**State Change**: `WAITING_HUMAN` -> `RUNNING`.

---

## 5. The Error Scenario (Dynamic Decision)

Suppose the next step fails unexpectedly.

### Step 5.1: Execute Restart (Node C)
**Worker**: "Running Restart..."
**Result**: **CRASH**. `ConnectionTimeout: AWS API not responding`.

### Step 5.2: The Crisis
**Worker**: "King, I have an error!" (`on_error` hook)

**King's Decision Matrix**:
1.  *Check Policy*: Is "Auto-Retry" enabled? Yes, but max retries (1) passed.
2.  *Check Severity*: Critical? Yes.
3.  *Decision*: **Do not crash.**
    *   Instead of moving to `FAILED`, the King decides to **Backtrack**.

**King's Action**:
1.  *Log*: "AWS Flake detected."
2.  *Strategy*: "I will pause and ask the user for a new credential or to retry manually."
3.  *Override*:
    *   Prevents Engine from terminating.
    *   Sets State: `PAUSED`.
    *   Sends HITL: "AWS API Failed. Retry?"

---

## 6. The Rescue (Resume)

**User**: "Retry" (and fixes VPN/Network).

**King (Action)**:
1.  *Rewind*: Resets Node C state.
2.  *Resume*: Tells Worker "Try Node C again."

**Outcome**:
*   Worker runs Node C.
*   Success!
*   Workflow completes.

---

## Summary of Roles

| Feature | **Execution Engine** (Worker) | **King Agent** (Manager) |
| :--- | :--- | :--- |
| **Focus** | How to do it (Code execution) | What to do (Strategy) |
| **Logic** | Deterministic (If X then Y) | Adaptive (If Error, Assess) |
| **State** | Running / Done | Running / Paused / Waiting / Rescue |
| **Error** | Raise Exception | Catch, Decide (Retry/Ask/Fail) |

This architecture allows the system to be **agentic** (smart decisions) while keeping the underlying execution **deterministic** (debuggable, reliable code).
