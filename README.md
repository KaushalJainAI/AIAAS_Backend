# AIAAS Backend (Django)

Welcome to the backend component of the **Agentic AI Automation System (AIAAS)**.

This repository runs the core logic for the visual workflow editor (Better n8n), the autonomous AI agent interactions, and the system integrations via the Model Context Protocol (MCP).

## Technical Architecture & Deep Dives

The backend is built around a **Supervisor-Worker** paradigm rather than a traditional linear script execution model. It employs a strict "Glass Box" execution pipeline, meaning every step, decision, and payload is visible, logged, and controllable.

To understand how this system truly operates, we have prepared detailed, step-by-step documentation located in the `docs/` directory. If you want to understand *how* AIAAS executes a workflow, start here:

### 1. The Compilation Engine
*   **[COMPILATION_PROCESS.md](./docs/COMPILATION_PROCESS.md)**
    *   Details the strict, single-pass compilation process found in `compiler/compiler.py`.
    *   Explains how visual layout JSON is converted into an executable **LangGraph StateGraph** in under 80 milliseconds.
    *   Covers the multi-layered validation (DAG, Credentials, Schema, Types).

### 2. The Execution Engine
*   **[EXECUTION_ENGINE.md](./docs/EXECUTION_ENGINE.md)**
    *   Details the runtime execution of workflows within LangGraph.
    *   Covers state management, deterministic node loops, and heartbeat monitoring.
    *   Explains the **Supervisor-Worker** paradigm where the AI (King) injects supervision hooks.

### 3. Security & Credentials
*   **[CREDENTIALS_AND_SECURITY.md](./docs/CREDENTIALS_AND_SECURITY.md)**
    *   Deep dive into the **AES-256 Symmetric Encryption** for API keys and secrets.
    *   Details the ownership validation logic that prevents cross-account credential leakage.
    *   Covers OAuth2 lifecycle management and automatic token refreshing.

### 4. AI Chat, Buddy & RAG
*   **[CHAT_AGENT.md](./docs/CHAT_AGENT.md)**
    *   Details the agentic tool loop, web search, deep research, and Python sandbox execution.
*   **[BUDDY_ASSISTANT.md](./docs/BUDDY_ASSISTANT.md)**
    *   Technical dive into the **BrowserOS integration**, heuristic command parsing, and multi-modal context capture.
*   **[MCP_INTEGRATION.md](./docs/MCP_INTEGRATION.md)**
    *   Details the **Model Context Protocol** client, connection pooling, and secure credential injection.
*   **[RAG_STRATEGY.md](./docs/RAG_STRATEGY.md)**
    *   Explains the Hierarchical RAG (File, User, Platform level) used in the chat system.

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
