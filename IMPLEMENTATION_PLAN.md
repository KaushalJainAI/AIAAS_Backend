# Workflow Compiler Backend - Implementation Plan

> Building on existing `ai_saas_platform` Django backend

---

# Part 1: The System Components

*What exists in the architecture*

```
┌─────────────────────────────────────────────────────────────────┐
│                         FRONTEND                                 │
│  Workflow Editor │ Credentials │ Logs │ Files │ Approvals UI    │
└────────────────────────────┬────────────────────────────────────┘
                             │ ▲
                             │ │ Human Feedback
                             ▼ │ (Approvals, Clarifications)
┌─────────────────────────────────────────────────────────────────┐
│                    DJANGO BACKEND                                │
│                                                                  │
│  ┌───────────────────────────────────────────────────────────┐  │
│  │                  👤 USER MANAGEMENT                        │  │
│  │         Auth • Permissions • API Keys • Usage             │  │
│  └───────────────────────────────────────────────────────────┘  │
│                                                                  │
│  ┌──────────────────────┐  ┌──────────────────────────────────┐ │
│  │    🧩 NODE SYSTEM    │  │       ⚙️ COMPILER                 │ │
│  │  Built-in + Custom   │  │  Parse • Validate • Build        │ │
│  └──────────────────────┘  └──────────────────────────────────┘ │
│                                                                  │
│  ┌──────────────────────┐  ┌──────────────────────────────────┐ │
│  │    ▶️ EXECUTOR        │  │       🤖 ORCHESTRATOR            │ │
│  │  Run Nodes in Order  │◀─┤  Supervise • Generate • Modify   │ │
│  └──────────┬───────────┘  │  ASK HUMAN • Handle Errors       │ │
│             │              └──────────────┬───────────────────┘ │
│             │                             │                      │
│             │  ┌──────────────────────────┴──────────────────┐  │
│             │  │          👥 HUMAN-IN-THE-LOOP               │  │
│             │  │  Approval Gate • Clarification • Recovery   │  │
│             │  │  ↕ Real-time Communication via WebSocket    │  │
│             │  └─────────────────────────────────────────────┘  │
│             │                                                    │
│  ┌─────────┐ ┌─────────┐ ┌─────────┐ ┌─────────┐ ┌──────────┐  │
│  │🔐 CREDS │ │🧠 INFER │ │📋 LOGS  │ │📡 STREAM│ │🏠 LOCAL  │  │
│  │Encrypted│ │RAG+Files│ │History  │ │SSE/WS   │ │LLM       │  │
│  └─────────┘ └─────────┘ └─────────┘ └─────────┘ └──────────┘  │
└─────────────────────────────────────────────────────────────────┘
```

---

# Part 2: The Data Flow

*How everything works together*

```
USER DESIGNS WORKFLOW
         │
         ▼
┌─────────────────┐
│  Frontend sends │──── { nodes: [...], edges: [...] }
│  JSON to Backend│
└────────┬────────┘
         │
         ▼
┌─────────────────┐     ┌──────────────────────┐
│    COMPILER     │────▶│  VALIDATION CHECKS   │
│  Parse JSON     │     │  ✓ DAG (no cycles)   │
│                 │     │  ✓ Credentials exist │
└────────┬────────┘     │  ✓ Types compatible  │
         │              └──────────────────────┘
         ▼
┌─────────────────┐
│  Build LangGraph│
│  execution plan │
└────────┬────────┘
         │
         ▼
┌─────────────────────────────────────────────────────────────┐
│                      ORCHESTRATOR                            │
│           Can STOP, PAUSE, MODIFY, or ASK HUMAN              │
└────────┬─────────────────────────────────────┬──────────────┘
         │                                     │
         │         ┌───────────────────────────┴────────────┐
         │         ▼                                        │
         │    ┌────────────────────────────────────┐        │
         │    │      👥 HUMAN-IN-THE-LOOP          │        │
         │    │                                    │        │
         │    │  🚨 APPROVAL NEEDED?               │        │
         │    │     → Sensitive operation          │        │
         │    │     → Database modification        │        │
         │    │     → External API call            │        │
         │    │                                    │        │
         │    │  ❓ CLARIFICATION NEEDED?          │        │
         │    │     → Ambiguous input              │        │
         │    │     → Missing parameters           │        │
         │    │     → Multiple valid options       │        │
         │    │                                    │        │
         │    │  ⚠️ ERROR RECOVERY?                │        │
         │    │     → Node failed, ask retry?      │        │
         │    │     → Unexpected result, proceed?  │        │
         │    │                                    │◀───────┤
         │    └────────────────┬───────────────────┘  Human │
         │                     │                      Response
         │    ◀────────────────┘                        │
         │    (Resume after human responds)             │
         ▼                                              │
┌─────────────────────────────────────────────────┐    │
│                   EXECUTOR                       │    │
│                                                  │    │
│  Node 1 ──▶ Node 2 ──▶ Node 3 ──▶ Node 4       │    │
│    │          │          │          │           │    │
│    └──────────┴──────────┴──────────┘           │    │
│              Data flows between                  │    │
└────────────────────┬────────────────────────────┘    │
                     │                                  │
    ┌────────────────┼────────────────┐                │
    ▼                ▼                ▼                ▼
┌────────┐     ┌──────────┐     ┌──────────┐     ┌──────────┐
│STREAMING│     │ LOGGING  │     │  CREDS   │     │ FRONTEND │
│push to  │     │ save to  │     │ fetch as │     │ Approval │
│frontend │     │ database │     │ needed   │     │ Requests │
└─────────┘     └──────────┘     └──────────┘     └──────────┘
```

---

# The Story of Each Component

## 👤 User Management
*"Who are you, and what can you do?"*

Every request starts here. The system knows who you are (JWT/API key), what tier you're on (free/pro/enterprise), and tracks your usage. Rate limits protect the system. Permissions ensure you only see your own workflows.

## 🧩 Node System  
*"The building blocks of automation"*

Each node is a self-contained Python class. **HTTP Request** knows how to call APIs. **Code** executes custom JavaScript/Python. **IF** routes data based on conditions. **OpenAI** talks to LLMs.

Users and AI can create **custom nodes**—upload a Python file, and it becomes a new block in the palette.

## ⚙️ Compiler
*"Ensuring your workflow will actually work"*

Before execution, the compiler checks everything:
- **DAG Check**: No loops, no orphan nodes
- **Credentials Check**: Do you have the API keys each node needs?
- **Type Check**: Does Node A's output match Node B's input?

Only valid workflows get compiled into LangGraph execution plans.

## ▶️ Executor
*"Running your automation, node by node"*

The executor walks through the graph, running each node in order. Data flows from one node to the next. Errors are caught and logged. Conditional nodes (IF, Switch) change the path.

## 🤖 Orchestrator
*"The supervisor that can ask for help"*

While workflows run, the orchestrator watches and **actively communicates with humans when needed**:

### Core Capabilities
- **Supervise**: Monitor workflow execution, detect issues
- **Generate**: Create workflows from natural language
- **Modify**: Adapt existing workflows dynamically
- **Control**: Stop, pause, resume at any point

### Human Feedback Triggers

**1. Approval Requests** (Blocking)
```
Orchestrator: "This workflow will delete 500 records from the database. 
              Approve or Cancel?"
User: "APPROVE" / "CANCEL"
```

**2. Clarification Questions** (Blocking)
```
Orchestrator: "I found 3 matching APIs. Which one should I use?
              1. OpenAI GPT-4
              2. Anthropic Claude
              3. Google Gemini"
User: "2"
```

**3. Error Recovery** (Optional blocking)
```
Orchestrator: "Node 'HTTP Request' failed with 429 Rate Limited.
              Options:
              1. Retry after 60 seconds
              2. Skip this node
              3. Stop workflow"
User: "1"
```

**4. Progress Updates** (Non-blocking)
```
Orchestrator: "✅ Step 3/5 complete. Scraped 150 products.
              Proceeding to data transformation..."
```

### Implementation Requirements
- WebSocket connection for real-time communication
- Notification queue for async approvals (email/push)
- Timeout handling (auto-proceed or auto-cancel)
- Audit trail for all human decisions

## 🔐 Credentials
*"Secrets, safely stored"*

API keys are encrypted in the database. When AI generates workflows, it uses placeholders—a **local LLM** (never sends secrets to cloud) maps those to your real credentials.

## 🧠 Inference Engine
*"Context for smarter decisions"*

Nodes can query a **knowledge base** (vector search) or your **uploaded files** to get context. An LLM node asking "summarize our Q3 sales" can actually retrieve the data.

## 📋 Logging
*"What happened, step by step"*

Every execution is logged. Every node: when it started, what input it got, what output it produced, how long it took, any errors. Full audit trail.

## 📡 Streaming
*"Live updates as it happens"*

SSE pushes events to the frontend: `node_start`, `node_complete`, `error`, `workflow_done`. The UI updates in real-time—you see the workflow progressing.

---

## 🔌 Custom Node Integration
*"Add any app you need"*

Custom nodes can integrate any new app. A user or AI writes a Python class:

```python
class TrelloNode(BaseNodeHandler):
    node_type = "custom_trello"
    fields = [
        FieldConfig("credential", "Trello API", FieldType.SELECT, required=True),
        FieldConfig("operation", "Action", FieldType.SELECT, options=[...]),
    ]
    
    async def execute(self, input_data, config, context):
        creds = await context.credentials.get(config["credential"])
        # Call Trello API with creds
        return {"result": response}
```

The node can:
- Define its own credential type
- Make HTTP calls to any API
- Access credentials, inference engine, logging
- Appear in the node palette like built-in nodes

---

## 🛡️ Error Handling
*"Graceful failures at every level"*

**Level 1: Compile Time**
- DAG validation fails → Error before execution starts
- Missing credentials → "Node X requires credentials"
- Type mismatch → "Output incompatible with input"

**Level 2: Runtime (Per-Node)**
```python
try:
    result = await node.execute(input, config, context)
except Exception as e:
    → Log error with full details
    → Stream error event to frontend
    → Decide: retry, skip, or stop workflow
```

**Level 3: Error Routing**
Nodes can have error output handles:
```python
outputs = [HandleDef("success"), HandleDef("error", color="red")]
```
User can design: "On error → Send Slack alert"

**Level 4: Orchestrator Intervention**
- Retry failed nodes (configurable retries)
- Stop after N failures
- Notify user: "Node X failed, pausing..."

---

# Part 3: Security Hardening

> 🚨 **Critical Issues Identified from Agentic-AI Backend Analysis**

The following security loopholes were discovered in the existing Agentic-AI implementation (`host.py`, `langgraph_super_agent.py`, `connections.py`). These MUST be addressed in the Django backend:

## 🔴 Critical Security Fixes

### 1. Authentication & Authorization
**Current Issue**: Flask endpoints have no auth - anyone can call `/chat`, `/clearMem`.

**Fix Required**:
```python
# core/authentication.py
class JWTAuthentication:
    """JWT-based authentication for API requests"""
    pass

class APIKeyAuthentication:
    """API key authentication for programmatic access"""
    pass

# Every endpoint must have:
@permission_classes([IsAuthenticated])
```

**Checklist**:
- [ ] JWT token generation/validation
- [ ] API key per user with rotation support
- [ ] Permission classes per endpoint
- [ ] Admin-only routes protection

---

### 2. Rate Limiting
**Current Issue**: No rate limiting = DoS vulnerability, cost explosion from LLM calls.

**Fix Required**:
```python
# core/middleware.py
from django_ratelimit.decorators import ratelimit

# Apply per-endpoint limits:
# - /compile: 10/minute  
# - /execute: 5/minute
# - /stream: 20 connections
```

**Tier-based limits**:
| Tier | Compile | Execute | Stream |
|------|---------|---------|--------|
| Free | 10/min | 5/min | 5 |
| Pro | 100/min | 50/min | 20 |
| Enterprise | Unlimited | 200/min | 100 |

---

### 3. Input Sanitization (Prompt Injection)
**Current Issue**: User input passed directly to LLM without sanitization.

**Fix Required**:
```python
# core/security.py
class InputSanitizer:
    """
    Sanitize user inputs before LLM processing:
    - Strip prompt injection patterns
    - Limit input length
    - Escape special characters
    - Block known malicious patterns
    """
    
    BLOCKED_PATTERNS = [
        r"ignore previous instructions",
        r"system prompt",
        r"</?(system|user|assistant)>",
    ]
```

---

### 4. Request Timeouts
**Current Issue**: No timeout on agent execution - can hang indefinitely.

**Fix Required**:
```python
# executor/runner.py
import asyncio

async def execute_with_timeout(workflow, timeout=300):
    try:
        result = await asyncio.wait_for(
            execute_workflow(workflow),
            timeout=timeout
        )
    except asyncio.TimeoutError:
        logger.error("Workflow execution timed out")
        return {"error": "Execution timeout", "success": False}
```

**Timeouts**:
- Workflow execution: 5 minutes (configurable)
- Individual node: 60 seconds
- HTTP requests: 30 seconds

---

### 5. Secrets Management
**Current Issue**: API keys in `.env` shared across all agents, logs may contain sensitive data.

**Fix Required**:
- [ ] Per-user credential isolation (already in checklist)
- [ ] Encryption at rest for credentials (AES-256)
- [ ] Audit logging for credential access
- [ ] Log sanitization (strip PII, secrets before logging)

```python
# core/logging.py
class SanitizedLogger:
    SENSITIVE_PATTERNS = [
        r"api_key[\"']?\s*[:=]\s*[\"'][^\"']+[\"']",
        r"password[\"']?\s*[:=]\s*[\"'][^\"']+[\"']",
        r"Bearer\s+[A-Za-z0-9\-._~+/]+=*",
    ]
    
    def sanitize(self, message):
        for pattern in self.SENSITIVE_PATTERNS:
            message = re.sub(pattern, "[REDACTED]", message)
        return message
```

---

### 6. CORS Configuration
**Current Issue**: No CORS headers configured.

**Fix Required**:
```python
# settings.py
CORS_ALLOWED_ORIGINS = [
    "https://yourfrontend.com",
]
CORS_ALLOW_CREDENTIALS = True
CORS_ALLOW_METHODS = ["GET", "POST", "PUT", "DELETE", "OPTIONS"]
```

---

### 7. Thread Safety
**Current Issue**: Shared orchestrator singleton across threads without locking.

**Fix Required**: 
- Use async execution context per-request
- Implement proper state isolation
- Use thread-local storage for user context

---

## 🟠 Architecture Improvements

### 8. Human-in-the-Loop Implementation
**Current Issue**: README mentions approval gates but they're not implemented.

**Fix Required**:
```python
# orchestrator/approval.py
class ApprovalGate:
    """
    Block execution for sensitive operations:
    - Database modifications
    - File operations
    - External API calls (non-whitelisted)
    - Credential access
    """
    
    APPROVAL_REQUIRED = [
        "database_write",
        "file_delete",
        "external_api",
        "credential_access",
    ]
    
    async def request_approval(self, user_id, operation, details):
        # Send notification to user
        # Block until approved or timeout
        pass
```

---

### 9. Message Queue for Scaling
**Current Issue**: In-memory queue loses messages on restart, max 5 messages.

**Fix Required**:
- Use Redis/Celery for task queue
- Persistent message storage
- Horizontal scaling support

---

### 10. Secure Agent Method Execution
**Current Issue**: Dynamic `getattr` allows LLM to potentially call any method.

**Fix Required**:
```python
# executor/safe_executor.py
ALLOWED_METHODS = {
    "Chatbot": ["chat"],
    "WebSearchingAgent": ["run", "search"],
    "DatabaseOrchestrator": ["query"],
}

def safe_execute(agent_name, method_name, *args, **kwargs):
    if method_name not in ALLOWED_METHODS.get(agent_name, []):
        raise SecurityError(f"Method {method_name} not allowed on {agent_name}")
    # proceed with execution
```

---

## 📊 Security Summary

| Priority | Issue | Status | Effort |
|----------|-------|--------|--------|
| 🔴 Critical | No Authentication | To Do | 4h |
| 🔴 Critical | No Rate Limiting | To Do | 2h |
| 🔴 Critical | Prompt Injection | To Do | 3h |
| 🔴 Critical | No Timeouts | To Do | 1h |
| 🟠 High | Secrets in Logs | To Do | 2h |
| 🟠 High | CORS Config | To Do | 0.5h |
| 🟠 High | Thread Safety | To Do | 3h |
| 🟠 High | Approval Gates | To Do | 4h |
| 🟡 Medium | Message Queue | To Do | 4h |
| 🟡 Medium | Safe Method Exec | To Do | 2h |

**Total Security Hardening: ~25.5 hours**

---

# Implementation Checklist

See [CHECKLIST.md](./CHECKLIST.md) for the full checklist (13 phases, 60+ items including security).

**Estimated: ~58.5 hours total (33h features + 25.5h security)**
