# Workflow Compiler Backend - Checklist

## ✅ Tech Stack (Confirmed)
- [x] LangGraph for graph execution
- [x] Celery + Redis for async tasks
- [x] Ollama (localhost) for local LLM
- [x] Django Channels for WebSocket HITL
- [x] Per-node adjustable timeouts
- [x] **LangChain Community Tools** (Wikipedia, etc.)

## 👤 User Management
- [x] UserProfile model (api_key, tier, limits) ✅
- [x] UsageTracking model ✅
- [x] APIKey model ✅
- [x] JWT authentication ✅
- [x] API key authentication middleware ✅
- [x] Permission classes ✅
- [x] Rate limiting ✅
- [x] Google OAuth SSO ✅

## 🧩 Node System
- [x] BaseNodeHandler class ✅
- [x] Trigger nodes (Manual, Webhook, Schedule) ✅
- [x] Core nodes (HTTP, Code, Set, If) ✅
- [x] LLM nodes (OpenAI, Gemini, Ollama) ✅
- [x] Integration nodes (Gmail, Slack, Sheets) ✅
- [x] NodeRegistry singleton ✅
- [x] CustomNode model ✅
- [x] Custom node validator ✅
- [x] Dynamic class loader ✅
- [x] **MCP Tool Node** ✅
- [x] **LangChain Tool Node** ✅

## ⚙️ Compiler
- [x] JSON parser ✅
- [x] DAG validator (cycles, orphans) ✅
- [x] Credential validator ✅
- [x] Type compatibility checker ✅
- [x] LangGraph builder ✅
- [x] Deterministic topological sort ✅

## ▶️ Executor
- [x] ExecutionContext class ✅
- [x] **ExecutionEngine** (`engine.py`) ✅
- [x] Node runner with error handling ✅
- [x] Data passing between nodes ✅
- [x] Conditional routing (If, Switch) ✅
- [x] Loop safety limits (max_loop_count) ✅
- [x] **Full Loop Support** (LoopNode, SplitInBatchesNode with batch slicing, result accumulation) ✅

## 🤖 Orchestrator
- [x] **KingOrchestrator** (`king.py`) ✅
- [x] Stop/Pause/Resume control ✅
- [x] AI workflow generator ✅
- [x] AI workflow modifier ✅
- [x] **Subworkflow Support** (nested execution) ✅
- [x] **Template & Versioning System** ✅
- [x] **Unified LLM Capabilities** (merged ai_generator into king.py) ✅
- [x] **Goal-Oriented Execution** (runtime context, goal conditions) ✅
- [x] **Configurable LLM Provider** (OpenRouter, OpenAI, Gemini) ✅
- [x] **Supervision Levels** (full, error_only, none) ✅

### Human Feedback Integration
- [x] WebSocket connection for real-time communication ✅
- [x] Approval request handler (blocking) ✅
- [x] Clarification question handler (blocking) ✅
- [x] Error recovery prompts (ask retry/skip/stop) ✅
- [x] Progress update streamer (non-blocking) ✅
- [x] Notification queue for async approvals (email/push) ✅
- [x] Timeout handling (auto-proceed or auto-cancel config) ✅
- [x] Human decision audit trail ✅

## 🔐 Credentials
- [x] Credential model (encrypted) ✅
- [x] Encryption/decryption utils ✅
- [x] CredentialManager class ✅
- [x] Local LLM credential injector ✅

## 🔌 MCP Integration (NEW)
- [x] MCPServer Model (STDIO/SSE) ✅
- [x] MCPClientManager (Connection handling) ✅
- [x] Tool Discovery API ✅
- [x] MCP Tool execution Node ✅
- [x] Integration with Registry ✅

## 📚 LangChain Integration (NEW)
- [x] LangChain Wrapper Node (`LangChainToolNode`) ✅
- [x] `langchain-community` integration ✅
- [x] Wikipedia tool support ✅

## 🧠 Inference Engine
- [x] Knowledge base (FAISS/Chroma) ✅
- [x] File upload + indexing ✅
- [x] RAG query pipeline ✅

## 📋 Logging
- [x] ExecutionLog model ✅
- [x] NodeExecutionLog model ✅
- [x] ExecutionLogger class ✅

## 📡 Streaming
- [x] StreamEvent model ✅
- [x] SSE broadcaster ✅
- [x] Progress tracker ✅

## 🛡️ Error Handling
- [x] Compile-time validation errors ✅
- [x] Runtime try/catch per node ✅
- [x] Error output handles for nodes ✅
- [x] Retry logic (configurable) ✅
- [x] Error event streaming (callback ready) ✅

## 🔒 Security Hardening (NEW - Critical)

### Authentication & Authorization
- [x] JWT token generation/validation ✅
- [x] API key per user with rotation support ✅
- [x] Permission classes per endpoint ✅
- [x] Admin-only routes protection ✅
- [x] Token refresh mechanism ✅

### Rate Limiting
- [x] Rate limit middleware setup ✅
- [x] Tier-based limits (Free/Pro/Enterprise) ✅
- [x] Per-endpoint limit configuration ✅
- [x] Rate limit headers in response ✅
- [x] Abuse detection and blocking ✅

### Input Sanitization
- [x] Prompt injection pattern detection ✅
- [x] Input length limits ✅
- [x] Special character escaping ✅
- [x] Blocked pattern list (updateable) ✅
- [x] Content policy enforcement ✅

### Timeouts
- [x] Workflow execution timeout (5 min default) ✅
- [x] Per-node timeout (60s default) ✅
- [x] HTTP request timeout (30s) ✅
- [x] Configurable timeout per node type ✅

### Secrets Management
- [x] Log sanitization (strip PII/secrets) ✅
- [x] Credential access audit logging ✅
- [x] Per-user credential isolation ✅
- [x] Encryption at rest (AES-256) ✅

### Infrastructure Security
- [x] CORS configuration ✅
- [x] CSP headers ✅
- [x] HTTPS enforcement ✅
- [x] Secure cookie settings ✅

### Thread Safety & Isolation
- [x] Async execution context per-request ✅
- [x] State isolation between users ✅
- [x] Thread-local storage for context ✅

### Approval Gates (Human-in-the-Loop)
- [x] ApprovalGate class ✅
- [x] Notification system for approvals ✅
- [x] Timeout for pending approvals ✅
- [x] Audit trail for approvals ✅

### Safe Execution
- [x] Whitelist of allowed methods per agent ✅
- [x] Method validation before execution ✅
- [x] Sandboxed code execution ✅

### Message Queue (Scaling)
- [x] Redis/Celery integration ✅
- [x] Persistent message storage ✅
- [x] Dead letter queue for failures ✅
- [x] Horizontal scaling support ✅

## 🧪 Testing
- [ ] Node unit tests
- [ ] Compiler tests
- [ ] Executor tests
- [ ] Integration tests
- [ ] API tests
- [ ] Security tests (auth, rate limiting)
- [ ] Prompt injection tests

---

## 🌐 Frontend-Driven APIs (NEW)

### AI Chat API
- [x] Chat message endpoint with streaming ✅
- [x] Conversation history storage ✅
- [x] History retrieval endpoint ✅
- [x] Context-aware responses (workflow/node) ✅

### Documents API
- [x] Document upload endpoint ✅
- [x] Document list/retrieve endpoints ✅
- [x] Document deletion ✅
- [x] RAG search integration ✅

### Insights/Analytics API
- [x] Execution statistics endpoint ✅
- [x] Per-workflow metrics ✅
- [x] Cost breakdown endpoint ✅
- [x] Credit usage tracking ✅

### Orchestrator Streaming API
- [x] WebSocket connection handler ✅
- [x] Real-time event broadcasting ✅
- [x] Pending HITL requests endpoint ✅
- [x] HITL response endpoint ✅
- [x] Thought history retrieval ✅

### Audit Trail API
- [x] Audit entry model ✅
- [x] Audit logging middleware ✅
- [x] Audit retrieval endpoints ✅
- [x] Audit export (CSV/JSON) ✅

### ⚠️ Missing Backend Endpoints (Frontend Needs These)
- [ ] **Forgot Password** - `POST /api/auth/forgot-password/`
- [ ] **Export Logs** - `GET /api/logs/export/` (CSV/JSON)
- [ ] **Notification Settings** - `GET/PATCH /api/settings/notifications/`
- [ ] **Billing API** - Usage stats, plan upgrade endpoints
  - [ ] `GET /api/billing/usage/` - Current usage stats
  - [ ] `GET /api/billing/plan/` - Current plan info
  - [ ] `POST /api/billing/upgrade/` - Plan upgrade
- [ ] **Insights Charts** - `GET /api/insights/charts/`

---

## 📊 Summary

| Phase | Items | Priority | Est. Hours |
|-------|-------|----------|------------|
| User Management | 6 | High | 4h |
| Node System | 9 | High | 8h |
| Compiler | 5 | High | 5h |
| Executor | 4 | High | 4h |
| Orchestrator | 4 | Medium | 4h |
| Credentials | 4 | High | 3h |
| Inference Engine | 3 | Medium | 3h |
| Logging | 3 | Medium | 2h |
| Streaming | 3 | Medium | 2h |
| Error Handling | 5 | High | 3h |
| **Security** | **30** | **Critical** | **25.5h** |
| Testing | 7 | High | 4h |
| **Frontend APIs** | **18** | **Medium** | **16.5h** |
| **Templates & Subworkflows** | **10** | **Critical** | **Completed** |
| **MCP Integration** | **5** | **High** | **Completed** |
| **LangChain Integration** | **3** | **High** | **Completed** |

**Total: ~94 hours**
