# Workflow Compiler Backend - Implementation Plan

> Building on existing `ai_saas_platform` Django backend

---

# Technical Decisions (Confirmed)

| Decision | Choice | Rationale |
|----------|--------|-----------|
| **Graph Execution** | LangGraph (LangChain) | Production-ready graph orchestration library |
| **Task Queue** | Celery + Redis | Long workflows async, short ones sync |
| **Schema Authority** | Backend-defined | Frontend adapts to backend node schemas |
| **Local LLM** | Ollama (localhost) | Credential mapping, local inference |
| **Real-time HITL** | Django Channels (WebSocket) | Already configured, real-time communication |
| **Timeouts** | Per-node adjustable | Default 60s, configurable in node settings |

## Execution Strategy
```
┌─────────────────────────────────────────────────────────┐
│  Workflow Received                                       │
│     │                                                    │
│     ├──▶ Quick (< 5 nodes, no LLM) ─▶ Sync execution    │
│     │                                                    │
│     └──▶ Long (5+ nodes or LLM)   ─▶ Celery async task  │
│               │                                          │
│               ▼                                          │
│          Redis Queue ──▶ Worker ──▶ LangGraph executor  │
└─────────────────────────────────────────────────────────┘
```

## Node Timeout Configuration
```python
# Each node can specify its own timeout
class NodeConfig:
    timeout_seconds: int = 60      # Default 60s
    retry_count: int = 0           # Number of retries
    retry_delay_seconds: int = 5   # Delay between retries
```

## Ollama Integration (Local LLM)
```python
# Used for:
# 1. Credential mapping (placeholder → real credential)
# 2. AI workflow generation/modification
# 3. Local inference nodes (no cloud API needed)

OLLAMA_BASE_URL = "http://localhost:11434"
OLLAMA_MODEL = "llama3.2"  # or mistral, codellama, etc.
```

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

## 🤖 Orchestrator Agent (JARVIS)
*"Your AI assistant for workflow automation"*

The Orchestrator is a **LangGraph ReAct agent** that uses your workflow system as its toolkit. It can create, run, modify, and combine automations via natural language.

### Architecture
```
┌─────────────────────────────────────────────────────────────────────────┐
│                    🤖 JARVIS ORCHESTRATOR AGENT                          │
│                                                                          │
│  ┌─────────────────────────────────────────────────────────────────────┐ │
│  │                    MEMORY SYSTEMS                                    │ │
│  │  📝 Conversation    🗂️ Knowledge      📚 Workflow       👤 User     │ │
│  │     Memory             Base            Templates       Preferences   │ │
│  └─────────────────────────────────────────────────────────────────────┘ │
│                                                                          │
│  ┌─────────────────────────────────────────────────────────────────────┐ │
│  │                    AGENT TOOLS                                       │ │
│  │  create_workflow │ execute_workflow │ modify_workflow │ pause       │ │
│  │  combine_workflows │ list_workflows │ schedule │ ask_human (HITL)   │ │
│  │  query_knowledge_base │ find_similar_templates                      │ │
│  └─────────────────────────────────────────────────────────────────────┘ │
└─────────────────────────────────────────────────────────────────────────┘
```

### Core Capabilities

| Capability | Description | Priority |
|------------|-------------|----------|
| **Create** | Generate workflows from natural language | 🟢 P0 |
| **Execute** | Run workflows, return results | 🟢 P0 |
| **Modify** | Change existing workflows via chat | 🟢 P0 |
| **Pause/Resume** | Control running executions | 🟢 P0 |
| **HITL** | Ask human for approval/clarification | 🟢 P0 |
| **Combine** | Merge or chain multiple workflows | 🟢 P0 |
| **Templates** | Suggest from learned patterns | 🟢 P0 |
| **Knowledge Base** | Query organizational docs | 🟡 P1 |
| **Schedule** | Set up recurring executions | 🟡 P1 |
| ~~Real-time Monitor~~ | ~~Watch running workflows~~ | 🔴 Deferred |

### Memory Systems

**1. Conversation Memory** (Easy)
```python
# Store all chats, summarize when > 20 messages
class ConversationMemory:
    def add_message(self, role, content)
    def get_context(self) -> list  # Returns relevant history
    def summarize_if_needed()      # LLM summarizes old messages
```

**2. Workflow Templates** (Medium)
```python
# Learn from successful workflows + n8n community
class WorkflowTemplate:
    name, description, nodes, edges
    source: 'user' | 'n8n' | 'system'
    embedding: vector for similarity search
    usage_count: int
```

**3. Knowledge Base** (Medium)
- Uses existing Document + DocumentChunk models
- RAG for organizational docs
- Agent tool: `query_knowledge_base(question)`

### Learning from n8n Workflows

```
n8n JSON ─▶ Parse ─▶ Map node types ─▶ Generate description ─▶ Embed ─▶ Store
```

**Node Type Mapping:**
```python
N8N_TO_OURS = {
    "n8n-nodes-base.httpRequest": "http_request",
    "n8n-nodes-base.slack": "slack",
    "n8n-nodes-base.openAi": "openai",
    # ... 200+ mappings
}
```

**Template Search:**
```python
@tool
def find_similar_templates(user_request: str) -> list:
    """Find workflow templates matching user's description"""
    # Embed request → search templates → return top 5
```

### Human Feedback Triggers

**1. Approval Requests** (Blocking)
```
Agent: "This will delete 500 records. Approve?"
User: "APPROVE" / "CANCEL"
```

**2. Clarification** (Blocking)
```
Agent: "Which API: 1) OpenAI 2) Claude 3) Gemini?"
User: "2"
```

**3. Error Recovery** (Blocking)
```
Agent: "HTTP failed with 429. Retry/Skip/Stop?"
User: "Retry"
```

### Implementation Phases

**Phase 1: Core Agent** (P0) ⏱️ ~2 weeks
- [ ] Basic chat via WebSocket
- [ ] Workflow tools (create, execute, pause, modify)
- [ ] Simple conversation memory (last 20 messages)
- [ ] HITL integration

**Phase 2: Memory & Templates** (P1) ⏱️ ~1 week
- [ ] Conversation summarization
- [ ] WorkflowTemplate model
- [ ] n8n workflow importer
- [ ] Template similarity search

**Phase 3: Knowledge Base** (P1) ⏱️ ~1 week
- [ ] Document embedding pipeline
- [ ] RAG query tool
- [ ] Context injection

### New Models Required

```python
# orchestrator/models.py - ADD THESE

class WorkflowTemplate(models.Model):
    """Learned workflow patterns from users and n8n"""
    name = models.CharField(max_length=200)
    description = models.TextField()
    source = models.CharField(choices=[('user','User'),('n8n','n8n'),('system','System')])
    nodes = models.JSONField()
    edges = models.JSONField()
    tags = models.JSONField(default=list)
    usage_count = models.IntegerField(default=0)
    embedding = models.BinaryField(null=True)
    created_at = models.DateTimeField(auto_now_add=True)

class ConversationSummary(models.Model):
    """Summarized conversation context for long chats"""
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    conversation_id = models.UUIDField()
    summary = models.TextField()
    key_entities = models.JSONField(default=list)
    embedding = models.BinaryField(null=True)
    created_at = models.DateTimeField(auto_now_add=True)
```

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
| 🔴 Critical | No Authentication | ✅ Done | 4h |
| 🔴 Critical | No Rate Limiting | ✅ Done | 2h |
| 🔴 Critical | Prompt Injection | ✅ Done | 3h |
| 🔴 Critical | No Timeouts | ✅ Done | 1h |
| 🟠 High | Secrets in Logs | ✅ Done | 2h |
| 🟠 High | CORS Config | ✅ Done | 0.5h |
| 🟠 High | Thread Safety | ✅ Done | 3h |
| 🟠 High | Approval Gates | ✅ Done | 4h |
| 🟡 Medium | Message Queue | ✅ Done | 4h |
| 🟡 Medium | Safe Method Exec | ✅ Done | 2h |

**Total Security Hardening: ~25.5 hours (COMPLETED)**

---

# Part 4: Frontend-Driven API Requirements

> Features implemented in the frontend that require backend API support

## 💬 AI Chat API
*"Backend for the AI assistant sidebar"*

The frontend has an AI Chat panel that requires:
- Streaming chat responses (SSE)
- Conversation history storage
- Context awareness (current workflow, selected node)

**Endpoints Required**:
```
POST /api/chat/message          # Send message, get streaming response
GET  /api/chat/history          # Get conversation history
DELETE /api/chat/history        # Clear conversation
GET  /api/chat/context/:workflowId  # Get workflow context
```

---

## 📁 Documents API
*"Knowledge base and file management"*

The frontend has a Documents page for knowledge management:

**Endpoints Required**:
```
GET    /api/documents           # List all documents
POST   /api/documents           # Upload document
GET    /api/documents/:id       # Get document content
DELETE /api/documents/:id       # Delete document
POST   /api/documents/search    # RAG search across documents
```

---

## 📊 Insights/Analytics API
*"Execution metrics and usage tracking"*

The frontend has an Insights dashboard showing:
- Execution counts over time
- Success/failure rates
- Credit usage per workflow
- API cost tracking

**Endpoints Required**:
```
GET /api/insights/executions    # Execution stats (daily/weekly/monthly)
GET /api/insights/workflows     # Per-workflow statistics
GET /api/insights/costs         # API cost breakdown
GET /api/insights/credits       # Credit usage history
```

---

## 🧠 Orchestrator Streaming API
*"Real-time AI thinking/planning visibility"*

The frontend has an Orchestrator page showing:
- AI thinking steps
- Planning decisions
- Execution progress
- Pending HITL actions

**Endpoints Required**:
```
WS   /ws/orchestrator/:executionId     # WebSocket for real-time updates
GET  /api/orchestrator/pending         # Get all pending HITL requests
POST /api/orchestrator/respond/:id     # Respond to HITL request
GET  /api/orchestrator/history/:id     # Get execution thought history
```

**Event Types** (pushed via WebSocket):
- `orchestrator:thinking` - AI is analyzing
- `orchestrator:planning` - AI is planning steps
- `orchestrator:executing` - Node execution started
- `orchestrator:waiting` - Awaiting human input
- `orchestrator:completed` - Step completed
- `orchestrator:error` - Error occurred

---

## 📜 Audit Trail API
*"Logging human decisions for compliance"*

Track all HITL decisions for audit purposes:

**Endpoints Required**:
```
GET /api/audit                  # List audit entries (paginated)
GET /api/audit/workflow/:id     # Audit entries for specific workflow
GET /api/audit/export           # Export audit log (CSV/JSON)
```

## 🧩 Templates API
*"Marketplace for workflow blueprints"*

**Endpoints Required**:
```
GET  /api/templates             # List available templates
GET  /api/templates/:id         # Get template details
POST /api/templates/search      # Semantic search
POST /api/templates/publish/:id # Publish workflow as template
```

**Security Features**:
- Credential scrubbing (removes API keys before publish)
- PII detection (warns about potential data leaks)

---

**Audit Entry Fields**:
- `timestamp`, `user_id`, `workflow_id`, `node_id`
- `action_type` (approval/clarification/error_recovery)
- `request_details`, `response`, `response_time_ms`

---

# Implementation Checklist

See [CHECKLIST.md](./CHECKLIST.md) for the full checklist (13 phases, 60+ items including security).

**Estimated: ~75 hours total (33h core + 25.5h security + 16.5h frontend APIs)**

