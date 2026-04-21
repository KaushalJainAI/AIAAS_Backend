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
| **External Tools** | MCP (Model Context Protocol) | Universal connection to external services (Stripe, Postgres, Filesystem) |

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

## MCP Integration (Model Context Protocol)
```python
# Used for:
# 1. Connecting to any MCP-compliant server (Node.js, Python, Go)
# 2. Exposing external tools to the workflow engine without custom code
# 3. Secure sandboxed tool execution via stdio or SSE

MCP_SERVER_TYPES = ['stdio', 'sse']
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
│  │  + MCP Clients       │  │                                  │ │
│  └──────────────────────┘  └──────────────────────────────────┘ │
│                                                                  │
│  ┌──────────────────────┐  ┌──────────────────────────────────┐ │
│  │    ▶️ WORKER ENGINE   │  │       👑 KING AGENT                │ │
│  │  Deterministic Run   │◀─┤  Plan • Supervise • HITL         │ │
│  │  (LangGraph)         │  │  Intent -> Execution             │ │
│  └──────────┬───────────┘  └──────────────┬───────────────────┘ │
│             │                             │                      │
│             │             ┌───────────────┴───────────────────┐  │
│             │             │      🔌 MCP INTEGRATION           │  │
│             │             │ Connects to: Stripe, GitHub, etc. │  │
│             │             └───────────────────────────────────┘  │
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
95: └─────────────────────────────────────────────────────────────────┘
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
│                      KING AGENT (ORCHESTRATOR)               │
│           Manage Intent, Pause/Resume, & HITL                │
└────────┬─────────────────────────────────────┬──────────────┘
         │                                     │
         │         ┌───────────────────────────┴────────────┐
         │         ▼                                        │
         │    ┌────────────────────────────────────┐        │
         │    │      🔌 MCP TOOL EXECUTION         │        │
         │    │                                    │        │
         │    │  1. Node asks for "stripe_refund"  │        │
         │    │  2. MCP Client connects to Server  │        │
         │    │  3. Sends JSON payload             │        │
         │    │  4. Returns Result                 │        │
         │    └────────────────────────────────────┘        │
         │                                                  │
         ▼                                                  │
┌─────────────────────────────────────────────────┐        │
│                   WORKER ENGINE                  │        │
│                                                  │        │
│  Node 1 ──▶ Node 2 ──▶ Node 3 ──▶ Node 4       │        │
│    │          │          │          │           │        │
│    └──────────┴──────────┴──────────┘           │        │
│              Data flows between                  │        │
└────────────────────┬────────────────────────────┘        │
                     │                                      │
    ┌────────────────┼────────────────┐                    │
    ▼                ▼                ▼                    ▼
┌────────┐     ┌──────────┐     ┌──────────┐         ┌──────────┐
│STREAMING│     │ LOGGING  │     │  CREDS   │         │ FRONTEND │
│push to  │     │ save to  │     │ fetch as │         │ Approval │
│frontend │     │ database │     │ needed   │         │ Requests │
└─────────┘     └──────────┘     └──────────┘         └──────────┘
```

---

# The Story of Each Component

## 🔌 MCP Integration
*"The universal connector"*

Instead of writing a custom Python class for every potential third-party service, we use the **Model Context Protocol (MCP)**.
- **MCP Servers**: External processes or URLs that expose a list of tools (e.g., `read_file`, `query_database`, `slack_post`).
- **MCP Client**: The backend connects to these servers on demand.
- **MCP Node**: A generic node that can execute ANY tool from ANY connected server.

This allows the platform to support hundreds of integrations day-one, just by pointing to an MCP server configuration.

## 👤 User Management & 🔐 Credentials
*"Who are you, what can you do, and what are your secrets?"*

Every request starts here. The system knows who you are, what tier you're on, and tracks your usage. Rate limits protect the system. Permissions ensure you only see your own workflows.

API keys are encrypted in the database using Fernet symmetric encryption and are strictly scoped to the creating user. When AI generates workflows, it uses placeholders—a local LLM maps those to your real credentials, which are only decrypted in-memory during execution.
> 📚 See [PERMISSIONS.md](./PERMISSIONS.md) for endpoint authorization details.
> 📚 See [CREDENTIALS_SYSTEM.md](./CREDENTIALS_SYSTEM.md) for the credential encryption and usage lifecycle.

## 🧩 Node System  
*"The building blocks of automation"*

Each node is a self-contained Python class. **HTTP Request** knows how to call APIs. **Code** executes custom Python. **IF** routes data based on conditions. **OpenAI** talks to LLMs. Users and AI can create **custom nodes**.
> 📚 See [NODES.md](./NODES.md) for the full registry of available nodes, loop support, and the node directory structure.

## ⚙️ Compiler
*"Ensuring your workflow will actually work"*

Before execution, the compiler checks the DAG for cycles, verifies credential ownership, and ensures type compatibility between nodes before building an executable LangGraph StateGraph.
> 📚 See [COMPILATION_PROCESS.md](./COMPILATION_PROCESS.md) for the exact steps and validation logic.

## ▶️ Execution Engine ("The Worker") & 🗃️ State Management
*"Deterministic, reliable execution"*

The execution engine (`engine.py`) takes a compiled graph and runs it deterministically. Data flow is strictly typed, moving from the global `WorkflowState` into isolated `ExecutionContext` sandboxes for each node to prevent malicious mutation.
> 📚 See [DRY_RUN.md](./DRY_RUN.md) for a complete step-by-step trace of a workflow execution from API payload to database completion.
> 📚 See [NODE_EXECUTION.md](./NODE_EXECUTION.md) for the micro-details of a single node's execution sandbox and expression evaluation.
> 📚 See [STATE_MANAGEMENT.md](./STATE_MANAGEMENT.md) for the schema definitions of `WorkflowState`, `ExecutionContext`, and `NodeItem` representing data flow.

## 👑 King Agent (Orchestrator)
*"The intelligent supervisor"*

The King (`king.py`) is the brain. It translates intent into workflow execution, supervises the worker, and handles human intervention (HITL).
> 📚 See [ORCHESTRATOR_CONTEXT_DESIGN.md](./ORCHESTRATOR_CONTEXT_DESIGN.md) for the orchestrator's memory hierarchy, dynamic context injection, and conversation capabilities.
> 📚 See [orchestrator.md](./orchestrator.md) for the high-level vision of the King Agent vs. the deterministic engine.

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
| **MCP** | Connect external tools dynamically | 🟢 P0 |

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
- [x] Basic chat via WebSocket
- [x] Workflow tools (create, execute, pause, modify)
- [x] Simple conversation memory (last 20 messages)
- [x] HITL integration

**Phase 2: Memory & Templates** (P1) ⏱️ ~1 week
- [x] Conversation summarization
- [x] WorkflowTemplate model
- [x] n8n workflow importer
- [x] Template similarity search

**Phase 3: Knowledge Base & MCP** (P1) ⏱️ ~1 week
- [x] Document embedding pipeline
- [x] RAG query tool
- [x] Context injection
- [x] MCP Client Manager
- [x] MCP Tool Node

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
    
# mcp_integration/models.py - ADDED
class MCPServer(models.Model):
    """External MCP Server Configuration"""
    name = models.CharField(max_length=255)
    type = models.CharField(choices=(('stdio', 'stdio'), ('sse', 'sse')))
    command = models.CharField(max_length=1024, blank=True)
    args = models.JSONField(default=list, blank=True)
    url = models.URLField(blank=True)
    env = models.JSONField(default=dict)
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

# Part 3: Additional Documentation

The monolithic implementation plan has been broken down to improve readability. 

Please refer to the following documents for detailed specifications on these topics:

*   🔒 **[Security Hardening](./SECURITY_HARDENING.md)**: Details the critical security fixes implemented from the Agentic-AI backend analysis (Auth, Rate Limiting, Prompt Injection, Timeouts, etc.) and architecture improvements.
*   🌐 **[Frontend API Requirements](./FRONTEND_API_REQUIREMENTS.md)**: Details the API endpoints required by the frontend application (Chat, Documents, Insights, Orchestrator Streaming, Audit Trail, Templates).
