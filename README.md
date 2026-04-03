# AIAAS Backend (Django)

Welcome to the backend component of the **Agentic AI Automation System (AIAAS)**.

This repository runs the core logic for the visual workflow editor (Better n8n), the autonomous AI agent interactions, and the system integrations via the Model Context Protocol (MCP).

## Technical Architecture & Deep Dives

The backend is built around a **Supervisor-Worker** paradigm rather than a traditional linear script execution model. It employs a strict "Glass Box" execution pipeline, meaning every step, decision, and payload is visible, logged, and controllable.

To understand how this system truly operates, we have prepared detailed, step-by-step documentation located in the `docs/archive/` directory. If you want to understand *how* AIAAS executes a workflow, start here:

### 1. The Core Lifecycle: A Full System Dry Run
*   **[DRY_RUN.md](./docs/archive/DRY_RUN.md)**
    *   **Read this first.** 
    *   This document follows a concrete "User Verification" workflow from when the user clicks "Execute" to when it finishes.
    *   It details the exact JSON payloads, database insertions (`ExecutionLog`), and how the deterministic worker loops through the graph.
    *   **Supervision**: Explains how the LLM supervisor injects itself into the loop via "Hooks" to safely **Pause/Resume** execution for Human-In-The-Loop (HITL) approvals.
    *   **Cleanup**: Explains the memory management and `Zombicide` cleanup background tasks.

### 2. Deep Dive: Inside a Node's Execution
*   **[NODE_EXECUTION.md](./docs/archive/NODE_EXECUTION.md)**
    *   Explains what happens *inside* the execution sandbox for a single node.
    *   Details how `{{ $input.xyz }}` expressions are resolved dynamically.
    *   Explains how isolated Python code executes safely inside a Custom Code node.

### 3. The Compilation Engine
*   **[COMPILATION_PROCESS.md](./docs/archive/COMPILATION_PROCESS.md)**
    *   Details the strict, single-pass compilation process found in `compiler/compiler.py`.
    *   Explains how visual layout JSON is converted directly into an executable `LangGraph StateGraph` in under 80 milliseconds.

### 4. Deployment Rules
*   **[WORKFLOW_DEPLOYMENT.md](./docs/WORKFLOW_DEPLOYMENT.md)**
    *   Details the strict criteria (Static Validation + Proof of Success) required before a workflow can be activated for automated running.

### 5. AI Chat Agent Architecture
*   **[CHAT_AGENT.md](./docs/CHAT_AGENT.md)**
    *   Details the "Perplexity-style" conversational AI engine.
    *   Covers the agentic tool loop, web search, deep research, and Python sandbox execution.
    *   Explains the two-tiered context/memory strategy for RAG and history.

### 6. Advanced RAG Strategy
*   **[RAG_STRATEGY.md](./docs/RAG_STRATEGY.md)**
    *   Explains the Hierarchical RAG (File, User, Platform level) used in the chat system.
    *   Details the automatic indexing of documents and the multi-tier retrieval logic.

---

## Local Development Setup

If you are running the backend in isolation (e.g., without the root repository's PowerShell orchestration scripts):

```bash
# 1. Create and activate a Virtual Environment
python -m venv venv
source venv/bin/activate  # On Windows use `venv\Scripts\activate`

# 2. Install dependencies
pip install -r requirements.txt

# 3. Setup the Database
python manage.py migrate
python manage.py createsuperuser

# 4. Run the Development Server
python manage.py runserver 0.0.0.0:8000
```

### Running Background Workers (Celery)
To execute background tasks and handle long-running timeouts safely:

In a separate terminal window:
```bash
celery -A workflow_backend worker -l info
```
