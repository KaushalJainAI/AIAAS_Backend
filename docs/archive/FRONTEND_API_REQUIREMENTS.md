# Frontend-Driven API Requirements

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

**Audit Entry Fields**:
- `timestamp`, `user_id`, `workflow_id`, `node_id`
- `action_type` (approval/clarification/error_recovery)
- `request_details`, `response`, `response_time_ms`

---

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
