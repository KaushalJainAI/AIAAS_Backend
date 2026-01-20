# Workflow Compiler Backend - Checklist

## 👤 User Management
- [ ] UserProfile model (api_key, tier, limits)
- [ ] UsageTracking model
- [ ] JWT authentication
- [ ] API key authentication
- [ ] Permission classes
- [ ] Rate limiting

## 🧩 Node System
- [ ] BaseNodeHandler class
- [ ] Trigger nodes (Manual, Webhook, Schedule)
- [ ] Core nodes (HTTP, Code, Set, If, Switch, Merge)
- [ ] LLM nodes (OpenAI, Gemini)
- [ ] Integration nodes (Gmail, Slack, Sheets)
- [ ] NodeRegistry singleton
- [ ] CustomNode model
- [ ] Custom node validator
- [ ] Dynamic class loader

## ⚙️ Compiler
- [ ] JSON parser
- [ ] DAG validator (cycles, orphans)
- [ ] Credential validator
- [ ] Type compatibility checker
- [ ] LangGraph builder

## ▶️ Executor
- [ ] ExecutionContext class
- [ ] Node runner with error handling
- [ ] Data passing between nodes
- [ ] Conditional routing (If, Switch)

## 🤖 Orchestrator
- [ ] WorkflowOrchestrator class
- [ ] Stop/Pause/Resume control
- [ ] AI workflow generator
- [ ] AI workflow modifier

### Human Feedback Integration
- [ ] WebSocket connection for real-time communication
- [ ] Approval request handler (blocking)
- [ ] Clarification question handler (blocking)
- [ ] Error recovery prompts (ask retry/skip/stop)
- [ ] Progress update streamer (non-blocking)
- [ ] Notification queue for async approvals (email/push)
- [ ] Timeout handling (auto-proceed or auto-cancel config)
- [ ] Human decision audit trail

## 🔐 Credentials
- [ ] Credential model (encrypted)
- [ ] Encryption/decryption utils
- [ ] CredentialManager class
- [ ] Local LLM credential injector

## 🧠 Inference Engine
- [ ] Knowledge base (FAISS/Chroma)
- [ ] File upload + indexing
- [ ] RAG query pipeline

## 📋 Logging
- [ ] ExecutionLog model
- [ ] NodeExecutionLog model
- [ ] ExecutionLogger class

## 📡 Streaming
- [ ] StreamEvent model
- [ ] SSE broadcaster
- [ ] Progress tracker

## 🛡️ Error Handling
- [ ] Compile-time validation errors
- [ ] Runtime try/catch per node
- [ ] Error output handles for nodes
- [ ] Retry logic (configurable)
- [ ] Error event streaming

## 🔒 Security Hardening (NEW - Critical)

### Authentication & Authorization
- [ ] JWT token generation/validation
- [ ] API key per user with rotation support  
- [ ] Permission classes per endpoint
- [ ] Admin-only routes protection
- [ ] Token refresh mechanism

### Rate Limiting
- [ ] Rate limit middleware setup
- [ ] Tier-based limits (Free/Pro/Enterprise)
- [ ] Per-endpoint limit configuration
- [ ] Rate limit headers in response
- [ ] Abuse detection and blocking

### Input Sanitization
- [ ] Prompt injection pattern detection
- [ ] Input length limits
- [ ] Special character escaping
- [ ] Blocked pattern list (updateable)
- [ ] Content policy enforcement

### Timeouts
- [ ] Workflow execution timeout (5 min default)
- [ ] Per-node timeout (60s default)
- [ ] HTTP request timeout (30s)
- [ ] Configurable timeout per node type

### Secrets Management
- [ ] Log sanitization (strip PII/secrets)
- [ ] Credential access audit logging
- [ ] Per-user credential isolation
- [ ] Encryption at rest (AES-256)

### Infrastructure Security
- [ ] CORS configuration
- [ ] CSP headers
- [ ] HTTPS enforcement
- [ ] Secure cookie settings

### Thread Safety & Isolation
- [ ] Async execution context per-request
- [ ] State isolation between users
- [ ] Thread-local storage for context

### Approval Gates (Human-in-the-Loop)
- [ ] ApprovalGate class
- [ ] Notification system for approvals
- [ ] Timeout for pending approvals
- [ ] Audit trail for approvals

### Safe Execution
- [ ] Whitelist of allowed methods per agent
- [ ] Method validation before execution
- [ ] Sandboxed code execution

### Message Queue (Scaling)
- [ ] Redis/Celery integration
- [ ] Persistent message storage
- [ ] Dead letter queue for failures
- [ ] Horizontal scaling support

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
- [ ] Chat message endpoint with streaming
- [ ] Conversation history storage
- [ ] History retrieval endpoint
- [ ] Context-aware responses (workflow/node)

### Documents API
- [ ] Document upload endpoint
- [ ] Document list/retrieve endpoints
- [ ] Document deletion
- [ ] RAG search integration

### Insights/Analytics API
- [ ] Execution statistics endpoint
- [ ] Per-workflow metrics
- [ ] Cost breakdown endpoint
- [ ] Credit usage tracking

### Orchestrator Streaming API
- [ ] WebSocket connection handler
- [ ] Real-time event broadcasting
- [ ] Pending HITL requests endpoint
- [ ] HITL response endpoint
- [ ] Thought history retrieval

### Audit Trail API
- [ ] Audit entry model
- [ ] Audit logging middleware
- [ ] Audit retrieval endpoints
- [ ] Audit export (CSV/JSON)

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

**Total: ~84 hours**

