# API Reference — Code Review Map

A per-endpoint index of the whole HTTP surface, built as a **starting point for
reviewing this codebase**. For every endpoint it records:

0. **URL** + method
1. **What** it does (one line)
2. **Access** — permission (global default is `IsAuthenticated`; only the exceptions are called out)
3. **Django app** — the app the route lives in
4. **Complexity** — rough cost of one call (see legend)
5. **Tested** — is there a unit/integration test (see per-app coverage note)
6. **Serializer** — DRF serializer(s) where one is used (`—` = raw `request.data`, function view)
7. **DB tables** — models the request reads or writes
8. **Atomic** — does the write run inside `transaction.atomic()`?
9. **Notes** — the one thing worth knowing before reading the code

> Hand-derived from `workflow_backend/urls.py`, each app's `urls.py`, `views.py`,
> `models.py`, and `test*.py` on 2026-07-24. `urls.py` remains the source of truth —
> **add a row whenever you add a route.**

## Legend

**Complexity**: `O(1)` single-row · `O(n)` linear · `Aggregate` DB GROUP BY ·
`External` blocking 3rd-party/LLM call · `Heavy` in-process ML/graph/agent loop ·
`Stream` long-lived SSE.

**Access**: `Auth` = `IsAuthenticated` (global default) · `Public` = `AllowAny` ·
`Admin` = `IsAdminUser`. API-key auth is accepted alongside JWT everywhere.

**Tested**: `✅` app has endpoint/integration tests covering this · `~` thin/indirect ·
`—` no direct test. See the coverage table below for depth per app.

**Atomic**: ⚠ **No view in this backend wraps writes in `transaction.atomic()`.**
Multi-step consistency, where it exists, is handled in service/engine layers, not the
view. Treat every write below as **non-atomic at the view layer** unless a service is
named — this is a systemic thing worth reviewing, so the column is omitted per-row and
called out here once.

## Global config (worth reading first)

- **Auth**: `rest_framework_simplejwt.JWTAuthentication` + `core.authentication.APIKeyAuthentication`.
- **Default permission**: `IsAuthenticated`. **Default throttle**: `anon 100/hr`,
  `user 1000/hr`, plus scoped `login 5/min`, `register 3/min`, `compile 10/min`,
  `password_reset 10/day` (⚠ aggressive — see audit report).
- **Docs** (`/api/schema/`, `/api/docs/`, `/api/redoc/`, `swagger.json`, `openapi.json`)
  are **admin-only** (`IsAdminUser`) — deliberately, so the API contract isn't public.
- **Health**: `GET /api/health/` — public, static JSON.

## Test coverage by app (as of 2026-07-24)

| App | Test files (≈#tests) | Depth |
|-----|----------------------|-------|
| `core` | tests (19), tests_cursor_pagination (2), integration/test_auth_flow (11) | ✅ strong (auth) |
| `compiler` | tests (47), integration/test_adversarial_compiler (13) | ✅ strong |
| `chat` | tests (16), tests_units (29), tests_rework (32), tests_pipeline (15) | ✅ strong |
| `nodes` | tests (2), tests_units (48) | ✅ strong (units) |
| `mcp_integration` | tests (4), tests_services (73), tests_units (22) | ✅ strong |
| `credentials` | tests (8), tests_units (9), integration/test_adversarial_credentials (11) | ✅ good |
| `inference` | tests (3), tests_units (12) | ✅ good |
| `orchestrator` | tests (4), tests_partial (5), tests_security (7), tests_units (9), integration/test_workflow_lifecycle (10) | ✅ good |
| `streaming` | tests (1), tests_units (14) | ~ moderate |
| `core`/executor engine | executor/tests/* (34) | ✅ (engine, not HTTP) |
| `browserOS` | tests (3) | ~ thin |
| `buddy` | tests (5) | ~ thin |
| `notifications` | tests (3) | ~ thin |
| `logs` | tests (1) | ~ thin |
| `templates` | tests (1) | ~ thin |
| `skills` | tests (1) | ~ thin |
| `canvas_agent` | — | — none |
| `imagine` | — | — none |

---

## 1. Core — auth, profile, API keys, usage — app: `core` — [core/views.py](../core/views.py)

Models: `UserProfile`, `APIKey`, `UsageTracking`, `PasswordOTP` (+ `auth.User`).

| URL | Method | What | Access | Complexity | Tested | Serializer | DB tables | Notes |
|-----|--------|------|--------|-----------|--------|-----------|-----------|-------|
| `/api/auth/register/` | POST | Register user | Public (3/min) | O(1) | ✅ | `UserRegistrationSerializer` | User, UserProfile | |
| `/api/auth/login/` | POST | JWT obtain | Public (5/min) | O(1) | ✅ | `CustomTokenObtainPairSerializer` | User | Wrong pw → 400 (audit: should be 401) |
| `/api/auth/google/` | POST | Google login | Public | External | ~ | — | User, UserProfile | |
| `/api/auth/token/refresh/` | POST | Refresh JWT | Public | O(1) | ~ | SimpleJWT | — | |
| `/api/auth/profile/` | GET/PUT/PATCH | Read/update profile | Auth | O(1) | ✅ | `UserProfileSerializer` | User, UserProfile | Email write silently ignored (audit) |
| `/api/auth/profile/avatar/` | POST | Upload avatar | Auth | O(1) | ~ | — | UserProfile | Multipart |
| `/api/auth/change-password/request-otp/` | POST | OTP to change pw | Auth-intended | O(1)+email | ~ | — | PasswordOTP | Returns 401 on empty body (audit) |
| `/api/auth/change-password/verify-otp/` | POST | Verify change OTP | Auth-intended | O(1) | ~ | — | PasswordOTP | |
| `/api/auth/change-password/` | POST | Change password | Auth | O(1) | ✅ | — | User, PasswordOTP | |
| `/api/auth/password-reset-request/` | POST | Reset OTP email | Public (10/day) | O(1)+email | ✅ | — | User, PasswordOTP | |
| `/api/auth/password-reset-verify/` | POST | Verify reset OTP | Public (10/day) | O(1) | ✅ | — | PasswordOTP | |
| `/api/auth/password-reset-confirm/` | POST | Set new password | Public (10/day) | O(1) | ✅ | — | User, PasswordOTP | |
| `/api/auth/api-keys/` | GET/POST | List/create API keys | Auth | O(1) | ✅ | `APIKeySerializer` | APIKey | Key shown once |
| `/api/auth/api-keys/{pk}/` | GET/PUT/PATCH/DELETE | API key CRUD | Auth (owner) | O(1) | ~ | `APIKeySerializer` | APIKey | |
| `/api/auth/api-keys/{pk}/rotate/` | POST | Rotate a key | Auth (owner) | O(1) | ~ | — | APIKey | |
| `/api/usage/` | GET/POST | Read/record usage | Auth | O(n) | ~ | `UsageTrackingSerializer` | UsageTracking | |
| `/api/usage/insights/` | GET | Usage aggregates | Auth | Aggregate | ~ | — | UsageTracking | |

---

## 2. Nodes registry — app: `nodes` — [nodes/views.py](../nodes/views.py)

Models: `CustomNode`, `AIProvider`, `AIModel`.

| URL | Method | What | Access | Complexity | Tested | Serializer | DB tables | Notes |
|-----|--------|------|--------|-----------|--------|-----------|-----------|-------|
| `/api/nodes/` | GET | List node schemas | Auth | O(n) | ✅ | `NodeSchemaSerializer` | CustomNode | |
| `/api/nodes/categories/` | GET | Nodes grouped by category | Auth | O(n) | ✅ | — | CustomNode | |
| `/api/nodes/models/` | GET | List AI models | Auth | O(n) | ✅ | `AIModelSerializer` | AIModel, AIProvider | |
| `/api/nodes/{node_type}/` | GET | One node schema | Auth | O(1) | ~ | `NodeSchemaSerializer` | CustomNode | |

---

## 3. Compiler — app: `compiler` — [compiler/views.py](../compiler/views.py)

| URL | Method | What | Access | Complexity | Tested | Serializer | DB tables | Notes |
|-----|--------|------|--------|-----------|--------|-----------|-----------|-------|
| `/api/workflows/{id}/compile/` | POST | Compile a saved workflow to a graph | Auth (10/min) | Heavy | ✅ | — | Workflow | Adversarial-tested |
| `/api/workflows/{id}/validate/` | POST | Validate a saved workflow | Auth (10/min) | Heavy | ✅ | — | Workflow | |
| `/api/compile/validate/` | POST | Ad-hoc validate nodes/edges | Auth | Heavy | ✅ | — | (none) | Empty input → 200 (audit: should be 400) |

---

## 4. Streaming (SSE) — app: `streaming` — [streaming/views.py](../streaming/views.py)

Model: `StreamEvent`.

| URL | Method | What | Access | Complexity | Tested | Serializer | DB tables | Notes |
|-----|--------|------|--------|-----------|--------|-----------|-----------|-------|
| `/api/streaming/executions/{uuid}/stream/` | GET | Live SSE of execution events | Auth | Stream | ✅ | — | StreamEvent | Long-lived connection |
| `/api/streaming/executions/{uuid}/events/` | GET | Event history for replay | Auth | O(n) | ✅ | `StreamEventSerializer` | StreamEvent | Invalid UUID → HTML 404 (audit) |
| `/api/streaming/status/` | GET | Connection status | Auth | O(1) | ~ | — | — | |
| `/api/streaming/executions/{uuid}/test/` | POST | Fire a test event | Auth (DEBUG) | O(1) | ~ | — | StreamEvent | Debug helper |

---

## 5. Orchestrator — workflows, executions, HITL, AI chat — app: `orchestrator` — [orchestrator/views.py](../orchestrator/views.py)

Models: `Workflow`, `WorkflowVersion`, `HITLRequest`, `ConversationMessage`,
`WorkflowTestResult`, `WorkflowCloneHistory`, `TriggerState`. All function-based views.

Agents live here too, on the same `Workflow` table with `kind='agent'` — see
[docs/AGENT_TEMPLATES.md](AGENT_TEMPLATES.md) §3 for why. Their routes are in
[orchestrator/agents.py](../orchestrator/agents.py), not `views.py`.

| URL | Method | What | Access | Complexity | Tested | Serializer | DB tables | Notes |
|-----|--------|------|--------|-----------|--------|-----------|-----------|-------|
| `/api/orchestrator/workflows/` | GET/POST | List / create workflow | Auth | O(n) | ✅ | `WorkflowSerializer` | Workflow | `nodes` accepts a string (audit gap 1) |
| `/api/orchestrator/workflows/{id}/` | GET/PUT/DELETE | Workflow detail | Auth (owner) | O(1) | ✅ | `WorkflowSerializer` | Workflow | **No PATCH** → 405 (audit) |
| `/api/orchestrator/workflows/{id}/deploy/` | POST | Deploy (activate triggers) | Auth | O(1) | ~ | — | Workflow, TriggerState | |
| `/api/orchestrator/workflows/{id}/undeploy/` | POST | Undeploy | Auth | O(1) | ~ | — | Workflow, TriggerState | |
| `/api/orchestrator/workflows/{id}/versions/` | GET | Version history | Auth | O(n) | ~ | `WorkflowVersionSerializer` | WorkflowVersion | |
| `/api/orchestrator/workflows/{id}/versions/{vid}/restore/` | POST | Restore a version | Auth | O(1) | ~ | — | Workflow, WorkflowVersion | |
| `/api/orchestrator/workflows/{id}/execute/` | POST | Run workflow | Auth | Heavy | ✅ | — | Workflow, ExecutionLog | Lifecycle integration-tested |
| `/api/orchestrator/executions/{id}/status/` | GET | Execution status | Auth | O(1) | ✅ | — | ExecutionLog | |
| `/api/orchestrator/executions/{id}/pause/` `…/resume/` `…/stop/` | POST | Execution control | Auth | O(1) | ~ | — | ExecutionLog | |
| `/api/orchestrator/hitl/pending/` | GET | Pending human-in-the-loop requests | Auth | O(n) | ✅ | `HITLRequestSerializer` | HITLRequest | Security-tested |
| `/api/orchestrator/hitl/{request_id}/respond/` | POST | Answer a HITL request | Auth | O(1) | ~ | — | HITLRequest | |
| `/api/orchestrator/chat/` `…/{cid}/` `…/{cid}/messages/{mid}/` | GET/POST/DELETE | AI workflow chat threads | Auth | External | ~ | — | ConversationMessage | LLM-backed |
| `/api/orchestrator/chat/context-aware/` | POST | Context-aware chat | Auth | External | ~ | — | ConversationMessage | |
| `/api/orchestrator/workflows/execute_partial/` (+`/{id}/…`) | POST | Test a single step | Auth | Heavy | ✅ | — | Workflow | `tests_partial` |
| `/api/orchestrator/ai/generate/` | POST | Generate a workflow from a prompt | Auth | External | ~ | — | Workflow | |
| `/api/orchestrator/workflows/{id}/ai/modify/` `…/ai/suggest/` | POST | AI modify / suggest | Auth | External | ~ | — | Workflow | |
| `/api/orchestrator/background-tasks/` | GET | Background task list | Auth | O(n) | ~ | — | (Celery/tasks) | |
| `/api/orchestrator/settings/update/` | POST | Update orchestrator settings | Auth | O(1) | ~ | — | UserProfile/settings | |
| `/api/orchestrator/system/info/` | GET | System info | Auth | O(1) | ~ | — | — | |
| `/api/orchestrator/executions/{id}/thoughts/` | GET | Orchestrator thought log | Auth | O(n) | ~ | — | OrchestratorThought | |
| `/api/orchestrator/workflows/{id}/test/` | POST | Run test suite for workflow | Auth | Heavy | ~ | — | WorkflowTestResult | |
| `/api/orchestrator/workflows/{id}/clone/` | POST | Clone a workflow | Auth | O(1) | ~ | — | Workflow, WorkflowCloneHistory | |
| `/api/orchestrator/agents/` | GET/POST | List / create agent (a `Workflow` with `kind='agent'`) | Auth | O(n) | ✅ | `AgentSerializer` | Workflow, ExecutionLog | `tests_agents`; stats counted from the log, not stored |
| `/api/orchestrator/agents/{id}/` | GET/PUT/PATCH/DELETE | Agent detail | Auth (owner) | O(1) | ✅ | `AgentSerializer` | Workflow | PATCH **merges** onto the stored config — a partial save must not reset an unsent grant |
| `/api/orchestrator/agents/{id}/execute/` | POST | Run an agent against a goal | Auth (owner) | Heavy | ✅ | `AgentExecuteSerializer` | Workflow, ExecutionLog | Synchronous; 402 when the spend cap is reached. Tools gated by `tool_grants` — [orchestrator/agent_runtime.py](../orchestrator/agent_runtime.py) |
| `/api/orchestrator/agents/{id}/approve/` | POST | Approve a tool call a run paused on | Auth (owner) | O(1) | ✅ | `AgentApproveSerializer` | Workflow | Ownership re-checked: a thread id is not an authorisation |
| `/api/webhooks/{user_id}/{path}` | POST | **Public** webhook receiver (triggers workflows) | **Public** | Heavy | ~ | — | Workflow, TriggerState, ExecutionLog | Only unauthenticated write path — review carefully |

---

## 6. Logs & insights — app: `logs` — [logs/views.py](../logs/views.py)

Models: `ExecutionLog`, `NodeExecutionLog`, `AuditEntry`, `OrchestratorThought`.

| URL | Method | What | Access | Complexity | Tested | Serializer | DB tables | Notes |
|-----|--------|------|--------|-----------|--------|-----------|-----------|-------|
| `/api/logs/insights/stats/` | GET | Execution statistics | Auth | Aggregate | ~ | — | ExecutionLog | |
| `/api/logs/insights/workflow/{id}/` | GET | Per-workflow metrics | Auth | Aggregate | ~ | — | ExecutionLog, NodeExecutionLog | |
| `/api/logs/insights/costs/` | GET | Cost breakdown | Auth | Aggregate | ~ | — | ExecutionLog | |
| `/api/logs/audit/` | GET | Audit trail list | Auth | O(n) | ~ | `AuditEntrySerializer` | AuditEntry | |
| `/api/logs/audit/export/` | GET | Export audit CSV | Auth | O(n) | ~ | — | AuditEntry | |
| `/api/logs/executions/` | GET | Execution history | Auth | O(n) | ~ | `ExecutionLogSerializer` | ExecutionLog | Disabled validation test (audit §11) |
| `/api/logs/executions/{id}/` | GET | Execution detail | Auth | O(1) | ~ | `ExecutionLogSerializer` | ExecutionLog, NodeExecutionLog | |
| `/api/logs/executions/{id}/activities/` | GET | Node activity logs | Auth | O(n) | ~ | — | NodeExecutionLog | |
| `/api/logs/executions/{id}/narrative/` | GET | Human-readable narrative | Auth | External | ~ | — | ExecutionLog, OrchestratorThought | LLM summary |

---

## 7. Inference / RAG — app: `inference` — [inference/views.py](../inference/views.py)

Models: `KnowledgeBase`, `Document`, `DocumentChunk`. Embeddings via
SentenceTransformer (all-MiniLM-L6-v2), FAISS index.

| URL | Method | What | Access | Complexity | Tested | Serializer | DB tables | Notes |
|-----|--------|------|--------|-----------|--------|-----------|-----------|-------|
| `/api/inference/kbs/` | GET/POST | List/create knowledge bases | Auth | O(n) | ✅ | `KnowledgeBaseSerializer` | KnowledgeBase | |
| `/api/inference/kbs/{id}/` | GET/PUT/DELETE | KB detail | Auth (owner) | O(1) | ✅ | `KnowledgeBaseSerializer` | KnowledgeBase | |
| `/api/inference/kbs/{id}/documents/{did}/assign/` | POST | Attach doc to KB | Auth | O(1) | ~ | — | KnowledgeBase, Document | |
| `/api/inference/kbs/{id}/documents/{did}/` | DELETE | Detach doc | Auth | O(1) | ~ | — | KnowledgeBase, Document | |
| `/api/inference/documents/` | GET/POST | List/upload documents | Auth | Heavy | ✅ | `DocumentSerializer` | Document, DocumentChunk | Upload chunks + embeds |
| `/api/inference/documents/{id}/` | GET/DELETE | Document detail | Auth (owner) | O(1) | ~ | `DocumentSerializer` | Document | |
| `/api/inference/documents/{id}/share/` | POST | Share document | Auth | O(1) | ~ | — | Document | |
| `/api/inference/documents/{id}/download/` | GET | Download original | Auth | O(1) | ~ | — | Document | |
| `/api/inference/rag/search/` | POST | Vector search | Auth | Heavy | ✅ | — | DocumentChunk | FAISS |
| `/api/inference/rag/query/` | POST | RAG answer (search + LLM) | Auth | External | ~ | — | DocumentChunk | |

---

## 8. Credentials — app: `credentials` — [credentials/views.py](../credentials/views.py)

Models: `CredentialType`, `Credential` (encrypted), `CredentialAuditLog`. Router-based
ViewSets. **IDOR-sensitive — verify per-user scoping when reviewing.**

| URL | Method | What | Access | Complexity | Tested | Serializer | DB tables | Notes |
|-----|--------|------|--------|-----------|--------|-----------|-----------|-------|
| `/api/credentials/` | GET/POST | List/create credentials | Auth (owner) | O(1) | ✅ | `CredentialSerializer` | Credential, CredentialType | `credential_type` = PK not slug (audit gap 6); secrets encrypted at rest |
| `/api/credentials/{pk}/` | GET/PUT/PATCH/DELETE | Credential CRUD | Auth (owner) | O(1) | ✅ | `CredentialSerializer` | Credential | Adversarial-tested |
| `/api/credentials/types/` | GET/POST/… | Credential-type registry | Auth | O(n) | ~ | `CredentialTypeSerializer` | CredentialType | |
| `/api/credentials/oauth/google/` | GET/POST/… | Google OAuth credential flow | Auth | External | ~ | — | Credential | |
| `/api/credentials/logs/` | GET | Audit logs | Auth | O(n) | ~ | `CredentialAuditLogSerializer` | CredentialAuditLog | Route 404s in audit (§7) — verify |

---

## 9. Templates (community) — app: `templates` — [templates/views.py](../templates/views.py)

Models: `WorkflowTemplate`, `WorkflowRating`, `WorkflowBookmark`, `TemplateComment`.

| URL | Method | What | Access | Complexity | Tested | Serializer | DB tables | Notes |
|-----|--------|------|--------|-----------|--------|-----------|-----------|-------|
| `/api/orchestrator/templates/` | GET | List templates | Auth | O(n) | ~ | `WorkflowTemplateSerializer` | WorkflowTemplate | |
| `/api/orchestrator/templates/{pk}/` | GET | Template detail | Auth | O(1) | ~ | `WorkflowTemplateSerializer` | WorkflowTemplate, TemplateComment | |
| `/api/orchestrator/templates/search/` | POST | Hybrid search | **Public** (line 159) | Heavy | ~ | `TemplateSearchSerializer` | WorkflowTemplate | 🔴 KeyError 500 (audit Bug 1) |
| `/api/orchestrator/templates/publish/{workflow_id}/` | POST | Publish workflow as template | Auth | O(1) | ~ | — | WorkflowTemplate, Workflow | |
| `/api/orchestrator/templates/{pk}/rate/` | POST | Rate a template | Auth | O(1) | ~ | `WorkflowRatingSerializer` | WorkflowRating | Disabled test (audit §11) |
| `/api/orchestrator/templates/{pk}/ratings/` | GET | List ratings | Auth | O(n) | ~ | `WorkflowRatingSerializer` | WorkflowRating | |
| `/api/orchestrator/templates/{pk}/bookmark/` | POST | Bookmark toggle | Auth | O(1) | ~ | — | WorkflowBookmark | |
| `/api/orchestrator/templates/{pk}/comments/` | GET/POST | Comments | Auth | O(n) | ~ | `TemplateCommentSerializer` | TemplateComment | |
| `/api/orchestrator/templates/{pk}/similar/` | GET | Similar templates | Auth | Heavy | ~ | — | WorkflowTemplate | |

---

## 10. MCP integration — app: `mcp_integration` — [mcp_integration/views.py](../mcp_integration/views.py)

Model: `MCPServer`. Router ViewSet. **Servers can execute code — security-sensitive.**

| URL | Method | What | Access | Complexity | Tested | Serializer | DB tables | Notes |
|-----|--------|------|--------|-----------|--------|-----------|-----------|-------|
| `/api/mcp/servers/` | GET/POST | List/create MCP servers | Auth (owner) | O(1) | ✅ | `MCPServerSerializer` | MCPServer | `tests_services` (73) |
| `/api/mcp/servers/{pk}/` | GET/PUT/PATCH/DELETE | Server CRUD | Auth (owner) | O(1) | ✅ | `MCPServerSerializer` | MCPServer | |
| `/api/mcp/servers/tools/` | GET | Aggregate tools from all servers | Auth | External | ~ | — | MCPServer | ⚠ Hangs (audit §6) — no timeout |

---

## 11. Skills — app: `skills` — [skills/views.py](../skills/views.py)

Model: `Skill`. Router ViewSet.

| URL | Method | What | Access | Complexity | Tested | Serializer | DB tables | Notes |
|-----|--------|------|--------|-----------|--------|-----------|-----------|-------|
| `/api/skills/` | GET/POST | List/create skills | Auth | O(1) | ~ | `SkillSerializer` | Skill | |
| `/api/skills/{pk}/` | GET/PUT/PATCH/DELETE | Skill CRUD | Auth (owner) | O(1) | ~ | `SkillSerializer` | Skill | |

---

## 12. Chat (standalone agent + guest) — app: `chat` — [chat/views.py](../chat/views.py), [chat/guest_views.py](../chat/guest_views.py)

Models: `ChatSession`, `ChatMessage`, `ChatAttachment`. Native tool-calling agent
([chat/agent.py](../chat/agent.py)) with a `SENSITIVE_TOOLS` HITL gate. Both send
endpoints run the same pipeline ([chat/pipeline.py](../chat/pipeline.py)) and
differ only in their event sink, so their behaviour cannot drift.

| URL | Method | What | Access | Complexity | Tested | Serializer | DB tables | Notes |
|-----|--------|------|--------|-----------|--------|-----------|-----------|-------|
| `/api/chat/sessions/` | GET/POST | List/create chat session | Auth | O(1) | ✅ | `ChatSessionSerializer` | ChatSession | Blank title accepted (audit gap 2) |
| `/api/chat/sessions/{pk}/` | GET/PUT/PATCH/DELETE | Session CRUD | Auth (owner) | O(1) | ✅ | `ChatSessionSerializer` | ChatSession, ChatMessage | |
| `/api/chat/sessions/{sid}/message/` | POST | Send message, wait for full reply | Auth | External | ✅ | — | ChatMessage, ChatSession | Same pipeline as the stream, events discarded |
| `/api/chat/sessions/{sid}/message/stream/` | POST | Send message (SSE stream) | Auth | Stream/External | ✅ | — | ChatMessage, ChatSession | Streams `content_chunk` token-by-token; auth is manual (DRF cannot wrap `StreamingHttpResponse`) |
| `/api/chat/sessions/{sid}/messages/{mid}/` | DELETE | Delete a message | Auth (owner) | O(1) | ~ | — | ChatMessage | |
| `/api/chat/sessions/{sid}/upload/` | POST | Upload attachment | Auth | O(1) | ~ | — | ChatAttachment | Multipart |
| `/api/chat/execute-tool/` | POST | Execute a chat tool | Auth | External | ✅ | — | — | Non-object `args` → 400; `SENSITIVE_TOOLS` → 403 so the HITL gate cannot be bypassed |
| `/api/chat/guest/sessions/` | POST | Create guest session | **Public** (IP-limited) | O(1) | ~ | — | ChatSession | NVIDIA NIM only |
| `/api/chat/guest/sessions/{sid}/` | GET | Get guest session | **Public** | O(1) | ~ | — | ChatSession, ChatMessage | |
| `/api/chat/guest/sessions/{sid}/message/stream/` | POST | Guest stream message | **Public** (IP-limited) | Stream/External | ~ | — | ChatMessage | |

---

## 13. Buddy (help assistant) — app: `buddy` — [buddy/views.py](../buddy/views.py)

⚠ No DRF serializers — raw `request.data` (audit §9.5). Writes to `OSWorkspace.theme_preferences`.

| URL | Method | What | Access | Complexity | Tested | Serializer | DB tables | Notes |
|-----|--------|------|--------|-----------|--------|-----------|-----------|-------|
| `/api/buddy/context/` | POST | Store UI context | Auth | O(1) | ~ | — | OSWorkspace | `{}` → 200 no validation (audit gap 3) |
| `/api/buddy/action/` | POST | Trigger a UI action | Auth | O(1) | ~ | — | — | Loose command parsing (audit gap 4) |
| `/api/buddy/commands/` | POST | Alias of action | Auth | O(1) | ~ | — | — | Same view as `action` |

---

## 14. BrowserOS — app: `browserOS` — [browserOS/views.py](../browserOS/views.py)

Models: `OSWorkspace`, `OSAppWindow`, `OSNotification`. Router ViewSets.

| URL | Method | What | Access | Complexity | Tested | Serializer | DB tables | Notes |
|-----|--------|------|--------|-----------|--------|-----------|-----------|-------|
| `/api/browseros/workspaces/` | GET/POST | List/create workspace | Auth (owner) | O(1) | ~ | `OSWorkspaceSerializer` | OSWorkspace | 🔴 POST → IntegrityError 500 (audit Bug 2) |
| `/api/browseros/workspaces/{pk}/` | GET/PUT/PATCH/DELETE | Workspace CRUD | Auth (owner) | O(1) | ~ | `OSWorkspaceSerializer` | OSWorkspace | `mine` action auto-creates |
| `/api/browseros/windows/` | GET/POST/… | App window CRUD | Auth (owner) | O(1) | ~ | `OSAppWindowSerializer` | OSAppWindow | |
| `/api/browseros/notifications/` | GET/POST/… | OS notification CRUD | Auth (owner) | O(1) | ~ | `OSNotificationSerializer` | OSNotification | |

---

## 15. Canvas agent — app: `canvas_agent` — ⚠ **DISABLED**

The app is commented out of `INSTALLED_APPS`, its `include()` is commented out of
[workflow_backend/urls.py](../workflow_backend/urls.py), and its WebSocket consumer is
commented out of [streaming/routing.py](../streaming/routing.py). **None of the routes below
are currently served** — they 404. The app code is still present in
[canvas_agent/](../canvas_agent/); re-enabling is a matter of uncommenting those three sites.

| URL | Method | What | Access | Complexity | Tested | Serializer | DB tables | Notes |
|-----|--------|------|--------|-----------|--------|-----------|-----------|-------|
| ~~`/api/canvas-agent/command/`~~ | POST | Run a canvas instruction (LangGraph) | Auth | Heavy/External | — | — | Workflow | Disabled. ⚠ Hangs, no timeout (audit §6) |
| ~~`/api/canvas-agent/node-types/`~~ | GET | Available node types | Auth | O(1) | — | — | CustomNode | Disabled |

WebSocket `ws/canvas-agent/` is likewise disabled.

---

## 16. Notifications — app: `notifications` — [notifications/views.py](../notifications/views.py)

Model: `Notification`. Router ViewSet (full CRUD).

| URL | Method | What | Access | Complexity | Tested | Serializer | DB tables | Notes |
|-----|--------|------|--------|-----------|--------|-----------|-----------|-------|
| `/api/notifications/` | GET/POST | List/create notification | Auth | O(1) | ~ | `NotificationSerializer` | Notification | 🔴 POST → IntegrityError 500 (audit Bug 3) — `perform_create` misses `user` |
| `/api/notifications/{pk}/` | GET/PUT/PATCH/DELETE | Notification CRUD | Auth (owner) | O(1) | ~ | `NotificationSerializer` | Notification | |
| `/api/notifications/{pk}/mark_read/` (action) | POST | Mark read | Auth (owner) | O(1) | ~ | — | Notification | |

---

## 17. Imagine (media generation) — app: `imagine` — [imagine/views.py](../imagine/views.py)

Models: `Generation`, `ImagineConversation`, `ImagineMessage`. Router ViewSets + agent APIViews.

| URL | Method | What | Access | Complexity | Tested | Serializer | DB tables | Notes |
|-----|--------|------|--------|-----------|--------|-----------|-----------|-------|
| `/api/imagine/agent/chat/` | POST | Media-gen agent chat | Auth | External | — | — | ImagineConversation, ImagineMessage | |
| `/api/imagine/agent/resume/` | POST | Resume agent run (after HITL) | Auth | External | — | — | ImagineConversation | |
| `/api/imagine/conversations/` | GET/POST/… | Conversation CRUD | Auth (owner) | O(1) | — | `ImagineConversationSerializer` | ImagineConversation, ImagineMessage | |
| `/api/imagine/` | GET/POST/DELETE | Generation list/create/delete | Auth (owner) | External | — | `GenerationSerializer` | Generation | Image/video/audio gen |
| `/api/imagine/{pk}/` | GET/DELETE | Generation detail | Auth (owner) | O(1) | — | `GenerationSerializer` | Generation | |

---

## Cross-cutting notes for reviewers

- **No view-level transactions.** Grep confirms zero `transaction.atomic` in any
  `views.py`. Any multi-write endpoint (workflow execute, RAG ingest, clone, deploy)
  relies on service/engine layers for consistency — **audit those, not the views.**
- **Two unauthenticated write paths:** `/api/webhooks/{user_id}/{path}` (triggers
  workflow execution) and the `chat/guest/*` endpoints (NVIDIA NIM, IP-limited).
  These are the highest-risk surfaces — review rate-limiting and input handling first.
- **Known crashes still open** (from the audit): template search (KeyError 500),
  browserOS workspace create (IntegrityError 500), notifications create
  (IntegrityError 500), and hanging endpoints (MCP `tools/`, canvas-agent `command/`).
  These rows are flagged 🔴/⚠ above.
- **`executor` app has views but no URL routing** — dead HTTP surface (audit §5.6);
  it's exercised only through the engine tests.
- **Thin-coverage apps** to prioritize for tests: `browserOS`, `buddy`, `canvas_agent`,
  `notifications`, `imagine`, `logs`, `templates`, `skills`.
