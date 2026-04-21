# Backend Split Plan: Three-System Architecture

> **Date**: 2026-03-06
> **Status**: Proposal
> **Authors**: System Architect

---

## Overview

The current monolithic Django backend (`Backend/`) will be decomposed into **three independent systems** that communicate via well-defined APIs. Each system has its own database, its own deployment, and its own security boundary.

```
┌────────────────────────────────────────────────────────────────────┐
│                    FRONTEND (React + Vite)                         │
│              Workflow Editor │ Chat │ Admin Panel                  │
└───────────┬──────────────────┬─────────────────┬──────────────────┘
            │                  │                 │
            ▼                  ▼                 ▼
┌───────────────────┐  ┌──────────────┐  ┌──────────────────┐
│  🌐 PLATFORM      │  │ 🏠 PERSONAL  │  │ 🚀 DEPLOYMENT    │
│  BACKEND          │  │ BACKEND      │  │ SYSTEM           │
│                   │  │              │  │                  │
│  Cloud-Hosted     │  │ Local Machine│  │ Dynamic Sandbox  │
│  Multi-Tenant     │  │ Single-User  │  │ Orchestration    │
│  Django           │  │ FastAPI      │  │ FastAPI + Docker  │
└───────────────────┘  └──────────────┘  └──────────────────┘
```

---

## System 1: 🌐 Platform Backend (Cloud)

**What it is**: The multi-tenant SaaS control plane. Manages users, workflows-as-data, AI features, and acts as the coordination hub between frontend and execution systems.

**Tech Stack**: Django + DRF (keep current), PostgreSQL, Redis, Celery Beat

**Runs on**: Cloud server (EC2, etc.)

### Modules Staying Here

| Current Module | What Stays | What Leaves |
|----------------|-----------|-------------|
| `core` | **Everything** — Auth, Users, API Keys, Usage Tracking | Nothing |
| `orchestrator` | Workflow CRUD, Version History, AI Chat, AI Generation, AI Modify, Conversation Messages, Clone, Export, System Info, Background task monitoring | `execute_workflow`, `execute_partial`, HITL response relay, Webhook receiver |
| `logs` | **Everything** — Execution History, Insights, Statistics, Audit trail, Cost breakdown | Node-level log *writing* (it receives logs, doesn't write them) |
| `streaming` | **Everything** — SSE streams, Event history, Connection status | Event *emission* (receives events from execution systems) |
| `templates` | **Everything** — Template CRUD, Search, Rate, Bookmark, Comments, Similar | Nothing |
| `inference` | **Everything** — Document CRUD, RAG Search, RAG Query | Nothing |
| `skills` | **Everything** — Skill CRUD | Nothing |
| `nodes` | Node Schema Registry — `NodeSchemaListView`, Categories, Detail, AI Models | Node *execution* handlers |
| `compiler` | `ValidateWorkflowView`, `AdHocValidateWorkflowView` (validation-only endpoints) | Actual compilation + graph building |

### Existing Endpoints That Stay (Unchanged)

```
# Core (Auth)
POST   /api/auth/register/
POST   /api/auth/login/
POST   /api/auth/google/
POST   /api/auth/token/refresh/
GET    /api/auth/profile/
PUT    /api/auth/change-password/
GET    /api/keys/
POST   /api/keys/
PUT    /api/keys/{id}/rotate/
GET    /api/usage/

# Orchestrator (Workflow CRUD + AI)
GET    /api/orchestrator/workflows/
POST   /api/orchestrator/workflows/
GET    /api/orchestrator/workflows/{id}/
PUT    /api/orchestrator/workflows/{id}/
DELETE /api/orchestrator/workflows/{id}/
POST   /api/orchestrator/workflows/{id}/deploy/
POST   /api/orchestrator/workflows/{id}/undeploy/
GET    /api/orchestrator/workflows/{id}/versions/
POST   /api/orchestrator/workflows/{id}/versions/
POST   /api/orchestrator/workflows/{id}/versions/{vid}/restore/
POST   /api/orchestrator/ai/generate/
POST   /api/orchestrator/workflows/{id}/ai/modify/
GET    /api/orchestrator/workflows/{id}/ai/suggest/
POST   /api/orchestrator/workflows/{id}/clone/
POST   /api/orchestrator/workflows/{id}/export/
POST   /api/orchestrator/workflows/{id}/test/
GET    /api/orchestrator/background-tasks/
PUT    /api/orchestrator/settings/update/
GET    /api/orchestrator/system/info/

# Orchestrator (Chat)
GET    /api/orchestrator/chat/
POST   /api/orchestrator/chat/
GET    /api/orchestrator/chat/{conversation_id}/
DELETE /api/orchestrator/chat/{conversation_id}/
POST   /api/orchestrator/chat/context-aware/

# Logs (Read-only surface for frontend)
GET    /api/logs/insights/stats/
GET    /api/logs/insights/workflow/{id}/
GET    /api/logs/insights/costs/
GET    /api/logs/audit/
GET    /api/logs/audit/export/
GET    /api/logs/executions/
GET    /api/logs/executions/{id}/
GET    /api/logs/executions/{id}/activities/
GET    /api/logs/executions/{id}/narrative/

# Streaming (SSE surface for frontend)
GET    /api/streaming/executions/{id}/stream/
GET    /api/streaming/executions/{id}/events/
GET    /api/streaming/status/

# Templates
GET    /api/orchestrator/templates/
GET    /api/orchestrator/templates/{id}/
GET    /api/orchestrator/templates/search/
POST   /api/orchestrator/templates/publish/{wf_id}/
POST   /api/orchestrator/templates/{id}/rate/
GET    /api/orchestrator/templates/{id}/ratings/
POST   /api/orchestrator/templates/{id}/bookmark/
GET    /api/orchestrator/templates/{id}/comments/
GET    /api/orchestrator/templates/{id}/similar/

# Inference (RAG)
GET    /api/inference/documents/
POST   /api/inference/documents/
GET    /api/inference/documents/{id}/
DELETE /api/inference/documents/{id}/
POST   /api/inference/documents/{id}/share/
GET    /api/inference/documents/{id}/download/
POST   /api/inference/rag/search/
POST   /api/inference/rag/query/

# Skills
GET    /api/skills/
POST   /api/skills/
GET    /api/skills/{id}/
PUT    /api/skills/{id}/
DELETE /api/skills/{id}/

# Node Registry (schemas only, not execution)
GET    /api/nodes/
GET    /api/nodes/categories/
GET    /api/nodes/models/
GET    /api/nodes/{node_type}/
```

### Endpoints That Change Behavior

These endpoints currently trigger execution directly. After the split, they become **dispatchers** that forward work to the Personal Backend or Deployment System:

```
# Execution becomes a dispatch to Personal/Deployment backend
POST   /api/orchestrator/workflows/{id}/execute/
  → Was: Calls KingOrchestrator.start() in-process
  → Now: Sends execution request to Personal Backend or Deployment System
         Receives execution_id back, stores in DB

# Partial execution proxied to Personal Backend
POST   /api/orchestrator/workflows/execute_partial/
POST   /api/orchestrator/workflows/{id}/execute_partial/
  → Was: Creates temp handlers and runs inline
  → Now: Forwards to Personal Backend's /execute-partial endpoint

# Execution control becomes relay commands
POST   /api/orchestrator/executions/{id}/pause/
POST   /api/orchestrator/executions/{id}/resume/
POST   /api/orchestrator/executions/{id}/stop/
GET    /api/orchestrator/executions/{id}/status/
  → Was: Calls KingOrchestrator.pause/resume/stop() in-memory
  → Now: Forwards command to whichever backend is running the execution

# HITL response becomes a relay
POST   /api/orchestrator/hitl/{id}/respond/
  → Was: Calls KingOrchestrator.submit_human_response() in-memory
  → Now: Forwards to the executing backend via its callback endpoint

# Thought history - reads from Platform DB (no change needed)
GET    /api/orchestrator/executions/{id}/thoughts/

# Webhook receiver becomes a dispatcher
POST   /api/webhooks/{user_id}/{path}
  → Was: Looks up trigger, calls execute_workflow_async()
  → Now: Looks up trigger, dispatches to the user's Personal Backend
```

### New Platform-Only Endpoints (Cross-System API)

These endpoints are called **by** the Personal Backend and Deployment System (not by the frontend):

```
# ── Inbound: Execution Status Reporting ──
# Called by Personal/Deployment backends to report execution progress

POST   /api/internal/executions/{id}/started/
  Body: { "runner_type": "personal|deployment", "runner_url": "..." }
  → Platform records which backend is running this execution

POST   /api/internal/executions/{id}/node-complete/
  Body: { "node_id": "...", "status": "completed|failed", 
          "output_data": {...}, "duration_ms": 123, "error": null }
  → Platform writes NodeExecutionLog, broadcasts SSE event

POST   /api/internal/executions/{id}/completed/
  Body: { "status": "completed|failed|cancelled", 
          "output_data": {...}, "error_message": null,
          "total_duration_ms": 456, "tokens_used": 789 }
  → Platform writes final ExecutionLog, broadcasts SSE event

POST   /api/internal/executions/{id}/thought/
  Body: { "node_id": "...", "thinking": "...", "thought": "...", "phase": "before|after|error" }
  → Platform writes OrchestratorThought, broadcasts to frontend

POST   /api/internal/executions/{id}/stream-event/
  Body: { "event_type": "node_start|node_complete|error|...", "data": {...} }
  → Platform broadcasts event to SSE subscribers

# ── Inbound: HITL Request Creation ──
# Called by Personal/Deployment backends when a node needs human input

POST   /api/internal/hitl/create/
  Body: { "execution_id": "...", "node_id": "...", "request_type": "approval|clarification",
          "title": "...", "message": "...", "options": [...], "timeout_seconds": 300,
          "callback_url": "http://personal-backend:8001/api/hitl/response/" }
  → Platform creates HITLRequest in DB, notifies user via WebSocket
  → When user responds, Platform POSTs back to callback_url

# ── Outbound: Execution Dispatch ──
# Platform calls Personal/Deployment backend to start work

POST   {personal_backend_url}/api/execute/
  Body: { "execution_id": "...", "workflow_json": {...}, "input_data": {...},
          "user_id": 123, "supervision_level": "full|failsafe|error_only|none",
          "skills": [...], "report_url": "https://platform.com/api/internal/",
          "goal": "...", "goal_conditions": {...} }

POST   {personal_backend_url}/api/execute-partial/
  Body: { "node_id": "...", "node_type": "...", "input_data": {...}, "config": {...} }

# ── Outbound: Execution Control ──
POST   {runner_url}/api/executions/{id}/pause/
POST   {runner_url}/api/executions/{id}/resume/
POST   {runner_url}/api/executions/{id}/stop/

# ── Internal Auth ──
# All internal endpoints are authenticated via a shared secret / mTLS
# Header: X-Internal-Secret: {SHARED_SECRET}
```

### King Orchestrator Split: What Stays on Platform

These methods of `KingOrchestrator` stay on or move to the Platform:

| Method | Stays/Moves | Reason |
|--------|-------------|--------|
| `create_workflow_from_intent()` | **Stays** | Uses templates, RAG, node registry — all platform data |
| `_decide_generation_strategy()` | **Stays** | Queries template DB |
| `_clone_template()` | **Stays** | DB operation |
| `_generate_workflow_from_scratch()` | **Stays** | LLM + node registry |
| `modify_workflow()` | **Stays** | LLM + node registry |
| `update_settings()` / `ensure_settings_loaded()` | **Stays** | User profile DB |
| `_call_llm()` (for generation/chat) | **Stays** | Design-time LLM usage |
| `start()` — the dispatch part | **Stays (modified)** | Creates ExecutionLog, but delegates engine to Personal/Deployment |
| `pause()` / `resume()` / `stop()` | **Stays (modified)** | Relays command to runner via HTTP |
| `get_status()` | **Stays** | Reads from DB |
| `submit_human_response()` | **Stays (modified)** | Writes DB, then forwards to runner callback |
| `_cleanup_loop()` / Zombicide | **Stays** | Monitors all executions centrally |

---

## System 2: 🏠 Personal Backend (Local Machine)

**What it is**: A lightweight, single-user execution engine that runs on the user's own machine. Handles credential storage, workflow compilation, and actual node execution with full local filesystem and MCP access.

**Tech Stack**: FastAPI (no Django dependency), SQLite (local credentials DB), no Celery needed

**Runs on**: User's local machine, Docker container, or dedicated VM

### Modules Moving Here

| Current Module | What Moves | Changes Required |
|----------------|-----------|-----------------|
| `compiler` | `WorkflowCompiler`, `validators.py`, `schemas.py`, `utils.py` | Remove Django ORM imports; credential validation via local DB |
| `executor/engine.py` | `ExecutionEngine` | Remove Django imports; report status via HTTP to Platform |
| `executor/king.py` (runtime part only) | `before_node()`, `after_node()`, `on_error()`, `_generate_thought()`, `ask_human()`, `_sanitize_data()` | Extract into `RuntimeSupervisor` class; HITL requests sent to Platform via HTTP |
| `executor/safe_execution.py` | **Everything** | No changes needed — already self-contained |
| `executor/credential_utils.py` | **Everything** | Rewrite to query local SQLite instead of Django ORM |
| `nodes/handlers/*` | All handler files: `base.py`, `core_nodes.py`, `integration_nodes.py`, `llm_nodes.py`, `logic_nodes.py`, `triggers.py`, `langchain_nodes.py`, `subworkflow_node.py`, `utility_nodes.py`, `registry.py` | Remove Django ORM dependencies; credentials from local store |
| `mcp_integration/client.py` | MCP Client logic | Runs locally — direct access to local MCP servers |
| `chat` | **Everything** — Standalone chat with LLM | Own local sessions, own conversation history |
| `credentials` | **Everything** — CRUD + encryption | Encryption uses local machine key; secrets never leave the machine |

### Credentials: The Local-First Model

```
CURRENT (Shared):                    NEW (Local):
┌──────────────┐                     ┌─────────────────────┐
│   Platform   │                     │  Personal Backend   │
│   Database   │                     │  (User's Machine)   │
│              │                     │                     │
│ ┌──────────┐ │                     │ ┌─────────────────┐ │
│ │ Fernet   │ │                     │ │ Local SQLite DB │ │
│ │ Encrypted│ │                     │ │                 │ │
│ │ Creds    │ │                     │ │ Encrypted with  │ │
│ │          │ │                     │ │ LOCAL machine   │ │
│ │ Shared   │ │                     │ │ key (DPAPI/     │ │
│ │ Django   │ │                     │ │ Keychain/       │ │
│ │ SECRET   │ │                     │ │ Secret Service) │ │
│ └──────────┘ │                     │ └─────────────────┘ │
└──────────────┘                     └─────────────────────┘
                                     Secrets NEVER leave
                                     the user's machine!
```

**Key security advantage**: The platform never sees decrypted API keys. Even in a data breach of the Platform DB, no credentials are exposed because they simply aren't there.

### Personal Backend Endpoints

```
# ── Execution ──
POST   /api/execute/
  Body: { "execution_id": "uuid", "workflow_json": {...}, 
          "input_data": {...}, "supervision_level": "full",
          "skills": [...], "report_url": "https://platform/api/internal/",
          "goal": "...", "goal_conditions": {...} }
  Response: { "accepted": true }
  → Starts async execution; reports progress to report_url

POST   /api/execute-partial/
  Body: { "node_id": "...", "node_type": "...", 
          "input_data": [...], "config": {...} }
  Response: { "success": true, "items": [...], "duration_ms": 123 }
  → Synchronous single-node test execution

# ── Execution Control ──
POST   /api/executions/{id}/pause/
POST   /api/executions/{id}/resume/
POST   /api/executions/{id}/stop/
GET    /api/executions/{id}/status/

# ── HITL Callback (from Platform) ──
POST   /api/hitl/response/
  Body: { "request_id": "...", "action": "approve|reject", "value": "..." }
  → Resumes paused execution with human's answer

# ── Credentials (Local CRUD) ──
GET    /api/credentials/
POST   /api/credentials/
GET    /api/credentials/{id}/
PUT    /api/credentials/{id}/
DELETE /api/credentials/{id}/
GET    /api/credentials/types/

# ── Standalone Chat (Local LLM) ──
GET    /api/chat/sessions/
POST   /api/chat/sessions/
POST   /api/chat/sessions/{id}/message/
DELETE /api/chat/sessions/{id}/messages/{msg_id}/
POST   /api/chat/sessions/{id}/upload/

# ── MCP Servers (Local) ──
GET    /api/mcp/servers/
POST   /api/mcp/servers/
PUT    /api/mcp/servers/{id}/
DELETE /api/mcp/servers/{id}/

# ── Health/Registration ──
GET    /api/health/
  Response: { "status": "ok", "version": "1.0.0", 
              "capabilities": ["code_exec", "file_access", "mcp", "local_llm"],
              "active_executions": 2 }

POST   /api/register/
  Body: { "platform_url": "https://platform.com", "user_token": "jwt..." }
  → Registers this Personal Backend instance with the Platform
  → Platform stores the callback URL for dispatching
```

### Runtime Supervisor (from king.py)

The execution-time supervision logic extracted from `KingOrchestrator`:

```python
# personal_backend/runtime_supervisor.py

class RuntimeSupervisor:
    """
    Extracted from KingOrchestrator — only the execution-time hooks.
    Runs IN-PROCESS with the engine on the Personal Backend.
    Reports thoughts and HITL requests to the Platform via HTTP.
    """

    def __init__(self, platform_report_url: str, execution_id: str, 
                 llm_config: dict = None):
        self.report_url = platform_report_url
        self.execution_id = execution_id
        self.llm_config = llm_config  # For _generate_thought
        self._pause_events = {}

    # ── OrchestratorInterface Implementation ──

    async def before_node(self, execution_id, node_id, node_type, 
                          context, input_data=None):
        """Pre-execution reasoning and gating."""
        thought = await self._generate_thought(...)
        await self._report_thought(node_id, thought, phase="before")
        # Check pause/cancel state
        ...
        return ContinueDecision()

    async def after_node(self, execution_id, node_id, result, context):
        """Post-execution analysis and loop protection."""
        # Loop counting
        # Goal condition checking
        await self._report_thought(node_id, thought, phase="after")
        return ContinueDecision()

    async def on_error(self, execution_id, node_id, node_type, 
                       error, context):
        """Error analysis — may request HITL via Platform."""
        if needs_human:
            response = await self._request_hitl(
                node_id, "error_recovery", error_message, 
                options=["Retry", "Skip", "Stop"]
            )
        ...

    # ── Communication with Platform ──

    async def _report_thought(self, node_id, thought, phase):
        """POST thought to Platform's internal API."""
        await httpx.post(f"{self.report_url}/executions/{self.execution_id}/thought/", 
                         json={...})

    async def _request_hitl(self, node_id, request_type, message, options):
        """
        POST HITL request to Platform. Platform notifies user.
        Then wait for Platform to POST back to our /api/hitl/response/ endpoint.
        """
        await httpx.post(f"{self.report_url}/hitl/create/", json={
            "execution_id": self.execution_id,
            "callback_url": f"http://localhost:8001/api/hitl/response/",
            ...
        })
        # Wait for callback...
        event = self._pause_events[request_id]
        await asyncio.wait_for(event.wait(), timeout=timeout)
        return event.response

    async def _report_node_complete(self, node_id, output, duration_ms):
        """POST node completion to Platform."""
        await httpx.post(
            f"{self.report_url}/executions/{self.execution_id}/node-complete/",
            json={...}
        )
```

---

## System 3: 🚀 Deployment System (Dynamic Sandboxes)

**What it is**: A dedicated system for running **deployed (production) workflows** in isolated, ephemeral sandboxes. When a workflow is deployed/activated (webhooks, schedules, polling triggers), the Deployment System creates a sandbox container for each execution.

**Tech Stack**: FastAPI + Docker SDK (docker-py), Container Orchestration (Docker Compose / K8s)

**Runs on**: Cloud server (same or separate from Platform)

### Why a Separate System?

| Concern | Personal Backend | Deployment System |
|---------|-----------------|-------------------|
| **Who triggers?** | User clicks "Execute" or "Test" | Webhooks, Schedules, Polling triggers |
| **User present?** | Yes — watching the execution | No — runs autonomously |
| **Trust level** | High — user's own machine | Lower — arbitrary webhook payloads |
| **Sandboxing** | Local machine isolation | Full container sandboxing per execution |
| **Scaling** | Single machine | Horizontally scalable |
| **Credentials** | Stored locally | Fetched from user's Personal Backend or encrypted bundle |
| **HITL** | Direct user interaction | Async notifications (email, WebSocket, timeout) |

### Modules in the Deployment System

| Module | What It Contains | Source |
|--------|-----------------|--------|
| Sandbox Manager | Creates/destroys Docker containers per execution | **New** |
| Worker Image | Pre-built Docker image with compiler, engine, node handlers | From `compiler/` + `executor/` + `nodes/` |
| Trigger Router | Receives webhooks, schedule ticks, poll results and dispatches | From `executor/trigger_manager.py` + `orchestrator/views.py:receive_webhook` |
| Credential Bridge | Fetches credentials from user's Personal Backend for the duration of an execution | **New** |

### Architecture

```
                         ┌──────────────────────────────┐
     Webhook arrives     │      Deployment System       │
    ──────────────────►  │                              │
                         │  ┌────────────────────────┐  │
     Schedule tick       │  │    Trigger Router      │  │
    ──────────────────►  │  │  (Webhook Registry,    │  │
                         │  │   Celery Beat,          │  │
     Poll trigger fires  │  │   Poll Manager)         │  │
    ──────────────────►  │  └───────────┬────────────┘  │
                         │              │               │
                         │              ▼               │
                         │  ┌────────────────────────┐  │
                         │  │   Sandbox Manager      │  │
                         │  │                        │  │
                         │  │   docker run           │  │
                         │  │    --rm                 │  │
                         │  │    --memory=512m        │  │
                         │  │    --cpus=1             │  │
                         │  │    --network=restricted │  │
                         │  │    aiaas-worker:latest  │  │
                         │  │                        │  │
                         │  └───────────┬────────────┘  │
                         │              │               │
                         │     ┌────────┴────────┐      │
                         │     ▼                 ▼      │
                         │  ┌───────┐         ┌───────┐ │
                         │  │Sandbox│         │Sandbox│ │
                         │  │  #1   │         │  #2   │ │
                         │  │       │         │       │ │
                         │  │Engine │         │Engine │ │
                         │  │+Nodes │         │+Nodes │ │
                         │  └───┬───┘         └───┬───┘ │
                         │      │                 │     │
                         └──────┼─────────────────┼─────┘
                                │                 │
                    Reports to Platform    Reports to Platform
```

### Deployment System Endpoints

```
# ── Trigger Ingestion (Public-facing) ──
POST   /api/webhooks/{user_id}/{path}
  → Looks up trigger config in Redis
  → Finds credential source (user's Personal Backend URL or cached bundle)
  → Spins up sandbox container with execution request
  Response: { "execution_id": "uuid", "status": "started" }

# ── Sandbox Management (Internal) ──
POST   /api/sandbox/create/
  Body: { "execution_id": "...", "workflow_json": {...}, 
          "credentials": {...}, "resource_limits": {...},
          "report_url": "https://platform/api/internal/" }
  → Starts Docker container with the worker image

GET    /api/sandbox/{execution_id}/status/
DELETE /api/sandbox/{execution_id}/

# ── Execution Control (from Platform relay) ──
POST   /api/executions/{id}/pause/
POST   /api/executions/{id}/resume/
POST   /api/executions/{id}/stop/

# ── Credential Bridge ──
POST   /api/credentials/fetch/
  Body: { "user_id": 123, "credential_ids": ["1", "5", "21"],
          "personal_backend_url": "http://user-machine:8001" }
  → Fetches credentials from user's Personal Backend
  → Returns encrypted bundle for injection into sandbox
  → Bundle is ephemeral (TTL = execution duration)

# ── Trigger Management (from Platform) ──  
POST   /api/triggers/register/
  Body: { "workflow_id": 45, "user_id": 123, "triggers": [
            { "type": "webhook", "path": "github/push", "config": {...} },
            { "type": "schedule", "cron": "0 9 * * *", "config": {...} },
            { "type": "polling", "interval_minutes": 5, "node_id": "...", "config": {...} }
          ],
          "personal_backend_url": "http://user-machine:8001" }

DELETE /api/triggers/unregister/{workflow_id}/

# ── Health ──
GET    /api/health/
  Response: { "status": "ok", "active_sandboxes": 3, 
              "capacity": 20, "version": "1.0.0" }
```

### Sandbox Worker Image

The sandbox is a minimal Docker image built from the Personal Backend's execution-only code:

```dockerfile
# Dockerfile.worker
FROM python:3.12-slim

# Only execution dependencies — no Django, no DB drivers
COPY compiler/ /app/compiler/
COPY executor/engine.py /app/executor/
COPY executor/safe_execution.py /app/executor/
COPY executor/runtime_supervisor.py /app/executor/
COPY nodes/handlers/ /app/nodes/handlers/
COPY mcp_integration/client.py /app/mcp_integration/

COPY worker_entrypoint.py /app/
COPY requirements-worker.txt /app/

RUN pip install -r /app/requirements-worker.txt

# Restricted: no root, no network except report_url
USER nobody
ENTRYPOINT ["python", "/app/worker_entrypoint.py"]
```

The `worker_entrypoint.py` reads execution params from environment/stdin, runs the engine, and reports results to the Platform via HTTP.

---

## Cross-System Communication Matrix

### Addressing Latency Concerns

> **Your concern about HTTP delays is valid.** Here's why it's manageable and what mitigations exist:

| Communication Pattern | Frequency | Latency Sensitivity | Mitigation |
|----------------------|-----------|---------------------|------------|
| Start execution | Once per execution | Low (user can wait 100ms) | Standard REST |
| Node completion report | Once per node | Low (not blocking engine) | **Fire-and-forget** — engine continues immediately, report is async |
| AI Thoughts | Once per node (if supervision=full) | Low | Fire-and-forget |
| SSE stream events | Once per node | Medium | **Batch buffer** — collect events for 100ms, send batch |
| Pause/Resume/Stop | Rare (user-initiated) | Medium | Standard REST |
| HITL Request | Rare | High (blocks execution) | WebSocket or long-poll with heartbeat |
| Credential fetch | Once at execution start | Medium | **Pre-fetched** before execution begins |

**Critical insight**: The engine **never blocks on reporting**. It runs `before_node → execute → after_node` synchronously in-process, and fires off status reports as background tasks. The only blocking HTTP call is HITL (which is blocking by design — we're waiting for a human).

```python
# Non-blocking reporting pattern
async def _report_async(self, url, data):
    """Fire-and-forget report. Engine never waits for Platform response."""
    asyncio.create_task(self._do_report(url, data))

async def _do_report(self, url, data):
    try:
        async with httpx.AsyncClient() as client:
            await client.post(url, json=data, timeout=5)
    except Exception as e:
        logger.warning(f"Failed to report to platform: {e}")
        self._pending_reports.append((url, data))  # Queue for retry
```

### Alternative: WebSocket for Real-Time Channel

For the **Personal Backend ↔ Platform** connection, instead of many individual HTTP calls, use a **persistent WebSocket**:

```
Personal Backend                          Platform Backend
     │                                         │
     │◄────── WebSocket Connection ────────────│
     │                                         │
     │  ──── execute command ─────────────►    │
     │  ◄──── node_complete event ─────────    │
     │  ◄──── thought event ───────────────    │
     │  ──── pause command ───────────────►    │
     │  ◄──── hitl_request event ──────────    │
     │  ──── hitl_response ───────────────►    │
     │                                         │
```

**Advantages**:
- Single connection, no per-request overhead
- True real-time bidirectional communication
- No HTTP latency for rapid node-to-node events
- Natural heartbeat mechanism (WebSocket ping/pong)

**Recommendation**: Use WebSocket as the **primary channel** between Personal Backend and Platform. Fall back to REST for startup registration and credential fetching.

### Full Communication Map

```
┌─────────────────────────────────────────────────────────────────────────┐
│                                                                         │
│   FRONTEND ◄──── REST + SSE ────► PLATFORM BACKEND                     │
│                                        │     │                          │
│                                        │     │                          │
│                       ┌────────────────┘     └──────────────────┐       │
│                       │                                         │       │
│                       ▼                                         ▼       │
│   PERSONAL BACKEND ◄──── WebSocket ────► PLATFORM              │       │
│        │                                    │                   │       │
│        │                                    │                   │       │
│        │                                    ▼                   │       │
│        │                          DEPLOYMENT SYSTEM ◄── REST ──►│       │
│        │                                    │                   │       │
│        │                                    │                   │       │
│        │◄────── REST (cred fetch) ──────────┘                   │       │
│                                                                         │
│   Legend:                                                               │
│   ─── REST: Registration, credential fetch, trigger management         │
│   ─── WebSocket: Real-time execution events, commands, HITL            │
│   ─── SSE: Frontend streaming (unchanged)                              │
│                                                                         │
└─────────────────────────────────────────────────────────────────────────┘
```

---

## Code Changes Required

### Phase 1: Interface Extraction (In Current Codebase — No Split Yet)

These changes prepare the codebase for separation without breaking anything:

**1.1. Create `ExecutionReporter` interface**
```python
# executor/reporting.py (NEW)
class ExecutionReporter(ABC):
    """Interface for reporting execution events."""
    
    async def report_node_start(self, execution_id, node_id, node_type): ...
    async def report_node_complete(self, execution_id, node_id, output, duration_ms, error=None): ...
    async def report_execution_complete(self, execution_id, status, output_data, error=None): ...
    async def report_thought(self, execution_id, node_id, thinking, thought, phase): ...
    async def report_stream_event(self, execution_id, event_type, data): ...

class LocalReporter(ExecutionReporter):
    """Current behavior — writes directly to Django ORM + broadcaster."""
    ...

class RemoteReporter(ExecutionReporter):
    """For Personal Backend — POSTs to Platform API."""
    ...
```

**1.2. Create `CredentialProvider` interface**
```python
# executor/credential_provider.py (NEW)
class CredentialProvider(ABC):
    """Interface for accessing credentials during execution."""
    
    async def get_workflow_credentials(self, user_id, workflow_json) -> dict: ...
    async def get_credential_ids(self, user_id) -> set[str]: ...

class DjangoCredentialProvider(CredentialProvider):
    """Current behavior — queries Django ORM."""
    ...

class LocalCredentialProvider(CredentialProvider):
    """For Personal Backend — queries local SQLite."""
    ...
```

**1.3. Split `king.py` into two classes**
```
executor/king.py (1718 lines)
  ├── executor/king_platform.py  (~1000 lines)
  │     └── KingOrchestrator (workflow generation, chat, dispatch, settings, 
  │                           template strategy, cleanup, status queries)
  │
  └── executor/runtime_supervisor.py  (~400 lines)
        └── RuntimeSupervisor (before_node, after_node, on_error, 
                              _generate_thought, ask_human, _sanitize_data,
                              pause/resume/stop event handling)
```

**1.4. Remove Django ORM imports from compiler**
```python
# compiler/compiler.py — CHANGE
# Before:
from credentials.models import Credential
cred_ids = set(Credential.objects.filter(user_id=user_id)...)

# After:
# Receives cred_ids as parameter (injected by whoever calls compile)
def compile(self, orchestrator=None, supervision_level=None, 
            credential_ids: set[str] = None):
```

**1.5. Remove Django ORM from logs/logger.py**
```python
# logs/logger.py — CHANGE
# Logger should use ExecutionReporter interface instead of direct ORM writes
class ExecutionLogger:
    def __init__(self, reporter: ExecutionReporter = None):
        self.reporter = reporter or LocalReporter()
```

### Phase 2: Create Personal Backend Service

**2.1. Project scaffold**
```
personal-backend/
├── app/
│   ├── main.py              ← FastAPI entrypoint
│   ├── config.py             ← Local settings (SQLite path, encryption key)
│   ├── routes/
│   │   ├── execution.py      ← /api/execute, /api/execute-partial
│   │   ├── credentials.py    ← /api/credentials CRUD
│   │   ├── chat.py           ← /api/chat sessions
│   │   ├── mcp.py            ← /api/mcp/servers
│   │   ├── control.py        ← /api/executions/{id}/pause|resume|stop
│   │   └── health.py         ← /api/health, /api/register
│   ├── engine/
│   │   ├── compiler.py       ← Copied from Backend/compiler/compiler.py (patched)
│   │   ├── engine.py         ← Copied from Backend/executor/engine.py (patched)
│   │   ├── runtime_supervisor.py  ← Extracted from king.py
│   │   ├── safe_execution.py ← Copied as-is
│   │   └── reporters.py      ← RemoteReporter + WebSocketReporter
│   ├── nodes/                ← Entire nodes/handlers/ directory (patched)
│   ├── credentials/
│   │   ├── models.py         ← SQLAlchemy models (local SQLite)
│   │   ├── encryption.py     ← Local machine key encryption
│   │   └── provider.py       ← LocalCredentialProvider
│   ├── mcp/
│   │   └── client.py         ← Copied from mcp_integration/client.py
│   └── chat/
│       ├── models.py         ← Local chat session storage
│       └── engine.py         ← LLM routing (from chat/views.py)
├── requirements.txt
├── Dockerfile
└── README.md
```

### Phase 3: Create Deployment System Service

**3.1. Project scaffold**
```
deployment-system/
├── app/
│   ├── main.py               ← FastAPI entrypoint
│   ├── config.py
│   ├── routes/
│   │   ├── webhooks.py        ← /api/webhooks/{user_id}/{path}
│   │   ├── sandbox.py         ← /api/sandbox/ CRUD
│   │   ├── triggers.py        ← /api/triggers/register|unregister
│   │   ├── control.py         ← /api/executions/{id}/pause|resume|stop
│   │   ├── credentials.py     ← /api/credentials/fetch (bridge)
│   │   └── health.py
│   ├── sandbox/
│   │   ├── manager.py         ← Docker SDK container lifecycle
│   │   ├── pool.py            ← Container pool for fast startup
│   │   └── resource_limits.py ← CPU/memory/network policies
│   ├── triggers/
│   │   ├── webhook_registry.py ← Redis-backed (from trigger_manager.py)
│   │   ├── scheduler.py        ← Celery Beat / APScheduler
│   │   └── poller.py           ← Polling trigger manager
│   └── worker/
│       ├── Dockerfile.worker   ← The sandbox image 
│       └── entrypoint.py       ← Worker bootloader
├── requirements.txt
├── docker-compose.yml          ← Redis + Deployment System + Worker image
└── README.md
```

### Phase 4: Platform Backend Modifications

**4.1. Add internal API endpoints** (`orchestrator/internal_views.py`)
- All the `/api/internal/*` endpoints listed above

**4.2. Modify execution dispatch** (`orchestrator/views.py:execute_workflow`)
- Instead of calling `KingOrchestrator.start()` in-process
- Create `ExecutionLog` in DB, determine target runner (Personal or Deployment)
- POST execution request to the runner

**4.3. Add agent registry** (`orchestrator/models.py`)
```python
class PersonalBackendRegistration(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    callback_url = models.URLField()  # e.g. http://192.168.1.5:8001
    ws_url = models.URLField(blank=True)  # WebSocket URL
    capabilities = models.JSONField(default=list)
    is_online = models.BooleanField(default=False)
    last_heartbeat = models.DateTimeField(null=True)
    registered_at = models.DateTimeField(auto_now_add=True)
```

**4.4. Modify execution control relay** (`orchestrator/views.py`)
- `pause_execution`, `resume_execution`, `stop_execution` now look up which runner holds the execution and forward the command

---

## Credential Flow: Deployment System ↔ Personal Backend

Deployed workflows need credentials, but credentials live on the user's Personal Backend:

```
1. Webhook arrives at Deployment System
2. Deployment System needs creds for workflow #45 (user #123)
3. Deployment System → Platform: "Where is user #123's Personal Backend?"
4. Platform → Deployment System: "http://192.168.1.5:8001"
5. Deployment System → Personal Backend: POST /api/credentials/execution-bundle/
   Body: { "workflow_json": {...}, "execution_id": "..." }
6. Personal Backend → Deployment System: 
   { "bundle": "<encrypted credential bundle>", "key": "<one-time key>" }
7. Deployment System injects bundle into sandbox container environment
8. Sandbox decrypts and uses credentials during execution
9. Bundle key expires after execution completes
```

**If Personal Backend is offline**:
- Option A: Execution queued until Personal Backend comes online
- Option B: User pre-authorizes a cached credential bundle with a TTL
- Option C: Deployment System notifies user that execution failed (credential unavailable)

---

## Migration Checklist

### Before Starting (Pre-Work)
- [ ] Audit all `from credentials.models import Credential` usages in compiler/executor
- [ ] Audit all `from logs.models import ExecutionLog` usages in compiler/executor
- [ ] Audit all Django ORM usages in `nodes/handlers/*.py`
- [ ] List all `sync_to_async` / `@sync_to_async` wrappers that wrap ORM calls

### Phase 1 (Interface Extraction — 2 weeks)
- [ ] Create `ExecutionReporter` interface + `LocalReporter` implementation
- [ ] Create `CredentialProvider` interface + `DjangoCredentialProvider`
- [ ] Split `king.py` → `king_platform.py` + `runtime_supervisor.py`
- [ ] Modify compiler to accept credential IDs as parameter
- [ ] Modify ExecutionLogger to use reporter interface
- [ ] Run all existing tests — nothing should break

### Phase 2 (Personal Backend — 3 weeks)
- [ ] Scaffold FastAPI project
- [ ] Port compiler with patched imports
- [ ] Port engine with RemoteReporter
- [ ] Port RuntimeSupervisor with HTTP/WebSocket reporting
- [ ] Port node handlers (remove ORM dependencies)
- [ ] Implement local credential store (SQLite + encryption)
- [ ] Port standalone chat
- [ ] Port MCP client
- [ ] Create Docker image
- [ ] E2E test: execute workflow from Personal Backend, report to Platform

### Phase 3 (Deployment System — 3 weeks)
- [ ] Scaffold FastAPI project
- [ ] Build sandbox manager (Docker SDK)
- [ ] Build worker Docker image
- [ ] Port trigger manager (webhook registry, scheduler, poller)
- [ ] Implement credential bridge
- [ ] Create internal API endpoints on Platform
- [ ] E2E test: webhook → sandbox → execution → Platform reporting

### Phase 4 (Platform Changes — 2 weeks)
- [ ] Add `/api/internal/*` endpoints
- [ ] Modify execution dispatch to forward to runners
- [ ] Add Personal Backend registration model + management UI
- [ ] Modify execution control to relay commands
- [ ] Modify webhook receiver to forward to Deployment System
- [ ] Frontend changes: credential management points to Personal Backend
- [ ] E2E integration test: all three systems working together

**Total estimated effort: 10 weeks**

---

## Summary Table

| Aspect | Platform Backend | Personal Backend | Deployment System |
|--------|-----------------|-----------------|-------------------|
| **Runs on** | Cloud (EC2) | User's machine | Cloud (alongside Platform) |
| **Framework** | Django + DRF | FastAPI | FastAPI + Docker SDK |
| **Database** | PostgreSQL | SQLite | Redis (triggers) |
| **Users** | Multi-tenant | Single-user | Multi-tenant |
| **Credentials** | ❌ None | ✅ Local encrypted | Fetched from Personal |
| **Execution** | ❌ Dispatches only | ✅ Interactive runs | ✅ Automated/sandboxed |
| **AI/Chat** | Workflow generation | Standalone chat | ❌ None |
| **HITL** | User-facing UI | In-process engine | Async notifications |
| **Triggers** | Stores configs | ❌ None | Runs webhooks/schedules |
| **Frontend talks to** | ✅ Primary API | Credential mgmt, chat | ❌ Never directly |
