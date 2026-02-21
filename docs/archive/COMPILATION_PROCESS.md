# Workflow Compilation Process

This document details how the backend transforms a user-defined workflow (JSON) into an executable runtime graph (LangGraph) in a single pass.

## Overview

The compilation process is unified into a single class `WorkflowCompiler` that performs validation and graph construction simultaneously to maximize performance.

```mermaid
graph TD
    JSON[Workflow JSON] -->|Input| WC[WorkflowCompiler]
    WC -->|Validates DAG & Configs| Check[Validation]
    Check -->|Pass| Graph[LangGraph StateGraph]
    Check -->|Fail| Err[WorkflowCompilationError]
    Graph -->|Invoke| Run[Execution Runtime]
```

## Unified Compilation

**Class:** `compiler.compiler.WorkflowCompiler`

This phase runs exactly when execution is requested (or during a dry-run check). It ensures the workflow is structurally sound, secure, and immediately executable.

### Steps:

1.  **Validation Phase**:
    *   **DAG Validation**: Checks for cycles and graph integrity.
    *   **Credential Validation**: Verifies user possesses referenced credentials.
    *   **Config Validation**: Checks node-specific required fields.
    *   **Type Compatibility**: Ensures data types match between connected nodes.
    *   **Fail Fast**: If any validation fails, a `WorkflowCompilationError` is raised immediately.

2.  **Graph Construction Phase**:
    *   **StateDefinition**: Initializes the `WorkflowState` schema (TypedDict).
    *   **Topo Sort**: Determines entry points.
    *   **Node Creation**: Wraps each node handler in an async wrapper that handles:
        *   Context Isolation (`ExecutionContext`)
        *   Input Resolution (from upstream outputs)
        *   Orchestrator Hooks (`before_node`, `after_node`, `on_error`)
        *   Loop Counting (`loop_stats`)
        *   Timeouts
    *   **Edge Creation**:
        *   Maps standard edges directly.
        *   Maps conditional nodes (`if`, `switch`, `loop`) to `add_conditional_edges` with dynamic routing logic based on output handles.

**Output**: An executable `CompiledStateGraph` (LangGraph Runnable).

## Runtime Execution

The `ExecutionEngine` is responsible for orchestrating the compilation and execution process. It instantiates the compiler and calls `compiler.compile(orchestrator=effective_orchestrator, supervision_level=supervision_level)`, passing the configured supervision level (`FULL`, `ERROR_ONLY`, or `NONE`) to dictate which orchestrator hooks are injected into the graph.

### Architecture:

*   **State**: A typed `WorkflowState` dictionary matches the execution context (variables, outputs, current node, status).
*   **Orchestrator Injection**: Depending on the `supervision_level`, the compiler selectively injects the `KingOrchestrator`'s hooks (`before_node`, `after_node`, `on_error`) into every node wrapper function, allowing for real-time AI supervision and control (Pause/Resume/Cancel).

## Example Flow

1.  **Orchestrator** (King) initiates execution and delegates to the **ExecutionEngine**.
2.  **ExecutionEngine** instantiates `WorkflowCompiler(json, user_creds)`.
3.  **Compiler** validates and builds the graph logically in <80ms, applying appropriate supervision hooks.
4.  **ExecutionEngine** invokes the graph, allowing LangGraph to execute the flow directly while reporting back to the Orchestrator via hooks.
