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

**Tested**: `` app has endpoint/integration tests covering this · `~` thin/indirect ·
`—` no direct test. See the coverage table below for depth per app.

**Atomic**: **No view in this backend wraps writes in `transaction.atomic()`.**
Multi-step consistency, where it exists, is handled in service/engine layers, not the
view. Treat every write below as **non-atomic at the view layer** unless a service is
named — this is a systemic thing worth reviewing, so the column is omitted per-row and
called out here once.

## Global config (worth reading first)

- **Auth**: `rest_framework_simplejwt.JWTAuthentication` + `core.authentication.APIKeyAuthentication`.
- **Default permission**: `IsAuthenticated`. **Default throttle**: `anon 100/hr`,
  `user 1000/hr`, plus scoped `login 5/min`, `register 3/min`, `compile 10/min`,
  `password_reset 10/day` ( aggressive — see audit report).
- **Docs** (`/api/schema/`, `/api/docs/`, `/api/redoc/`, `swagger.json`, `openapi.json`)
  are **admin-only** (`IsAdminUser`) — deliberately, so the API contract isn't public.
- **Health**: `GET /api/health/` — public, static JSON.

## Test coverage by app (as of 2026-08-18)

| App | Test files (≈#tests) | Depth |
|-----|----------------------|-------|
| `core` | tests (19), tests_cursor_pagination (2), integration/test_auth_flow (11) |  strong (auth) |
| `compiler` | tests (47), integration/test_adversarial_compiler (13) |  strong |
| `chat` | tests (16), tests_units (29), tests_rework (32), tests_pipeline (15) |  strong |
| `nodes` | tests (2), tests_units (48) |  strong (units) |
| `mcp_integration` | tests (4), tests_services (73), tests_units (22), tests_connections (16), tests_credential_bridge (9), tests_fresh_install (8) |  strong |
| `credentials` | tests (8), tests_units (9), integration/test_adversarial_credentials (11) |  good |
| `inference` | tests (3), tests_units (12), test_extraction (12), test_extract_task (12) — extraction engine (LLM per-document with threshold) + merge migration 0009; test_fulltext_backend, test_backends, test_kb_backends_api — retrieval backends (2026-08-24); test_regressions (38) — the audit fixes of 2026-08-24: KB resolution, fail-fast retrieval, file_type vocabulary, delete-side stats, hybrid stats, row write surface, duplicate-name message, session-KB id stability, share ordering, posting-scan order; test_filesystem (42) + test_recycle (28) — the per-user file system (2026-08-25): isolation table, choke point, tree mechanics, recycle bin and the 30-day sweep; chat/tests/test_knowledge_tools — retrieval tool routing and trash invisibility |  strong |
| `orchestrator` | tests (4), tests_partial (5), tests_security (7), tests_units (9), test_agent_runtime |  good — `integration/test_workflow_lifecycle` and `integration/test_adversarial_orchestrator` deleted 2026-08-18 (they exercised the retired Workflow model / `/api/orchestrator/workflows/` routes) |
| `streaming` | tests (1), tests_units (14) | ~ moderate |
| `core`/executor engine | executor/tests/* (34) |  (engine, not HTTP) |
| `notifications` | tests (3) + tests_reminders (20) |  reminders; ~ thin elsewhere |
| `logs` | tests (69 across 5 files) |  strong — every route + turn/delegation/revision semantics + the 0015 backfill |
| `templates` | tests (1) | ~ thin |
| `skills` | tests (1) | ~ thin |
| `imagine` | tests_catalog (20), tests_api (9), tests_dispatcher (10), tests_intent (7) |  catalog + HTTP contract + async split |
| `eval` | test_graders (31), test_supervision (19), test_runner (12), test_api (37), test_public_api (14) |  strong — graders, every supervision policy, sweep abort/cancel, every route, plus the importability contract of `eval/api.py` |

---

## 1. Core — auth, profile, API keys, usage — app: `core` — [core/views.py](../core/views.py)

Models: `UserProfile`, `APIKey`, `UsageTracking`, `PasswordOTP` (+ `auth.User`).

| URL | Method | What | Access | Complexity | Tested | Serializer | DB tables | Notes |
|-----|--------|------|--------|-----------|--------|-----------|-----------|-------|
| `/api/auth/register/` | POST | Register user | Public (3/min) | O(1) |  | `UserRegistrationSerializer` | User, UserProfile | |
| `/api/auth/login/` | POST | JWT obtain | Public (5/min) | O(1) |  | `CustomTokenObtainPairSerializer` | User | Wrong pw → 400 (audit: should be 401) |
| `/api/auth/google/` | POST | Google login | Public | External | ~ | — | User, UserProfile | |
| `/api/auth/token/refresh/` | POST | Refresh JWT | Public | O(1) | ~ | SimpleJWT | — | |
| `/api/auth/profile/` | GET/PUT/PATCH | Read/update profile | Auth | O(1) |  | `UserProfileSerializer` | User, UserProfile | Email write silently ignored (audit). Writes `vision_provider`/`vision_model` — the witness `ask_vision` calls (docs/VISION_AGENT.md) |
| `/api/auth/profile/avatar/` | POST | Upload avatar | Auth | O(1) | ~ | — | UserProfile | Multipart |
| `/api/auth/change-password/request-otp/` | POST | OTP to change pw | Auth-intended | O(1)+email | ~ | — | PasswordOTP | Returns 401 on empty body (audit) |
| `/api/auth/change-password/verify-otp/` | POST | Verify change OTP | Auth-intended | O(1) | ~ | — | PasswordOTP | |
| `/api/auth/change-password/` | POST | Change password | Auth | O(1) |  | — | User, PasswordOTP | |
| `/api/auth/password-reset-request/` | POST | Reset OTP email | Public (10/day) | O(1)+email |  | — | User, PasswordOTP | |
| `/api/auth/password-reset-verify/` | POST | Verify reset OTP | Public (10/day) | O(1) |  | — | PasswordOTP | |
| `/api/auth/password-reset-confirm/` | POST | Set new password | Public (10/day) | O(1) |  | — | User, PasswordOTP | |
| `/api/auth/api-keys/` | GET/POST | List/create API keys | Auth | O(1) |  | `APIKeySerializer` | APIKey | Key shown once |
| `/api/auth/api-keys/{pk}/` | GET/PUT/PATCH/DELETE | API key CRUD | Auth (owner) | O(1) | ~ | `APIKeySerializer` | APIKey | |
| `/api/auth/api-keys/{pk}/rotate/` | POST | Rotate a key | Auth (owner) | O(1) | ~ | — | APIKey | |
| `/api/usage/` | GET/POST | Read/record usage | Auth | O(n) | ~ | `UsageTrackingSerializer` | UsageTracking | |
| `/api/usage/insights/` | GET | Usage aggregates | Auth | Aggregate | ~ | — | UsageTracking | |

---

## 2. Nodes — app: `nodes` — **deleted 2026-08-19**

No routes, no models, no `views.py`, no `urls.py`. The three schema endpoints
(`/api/nodes/`, `/api/nodes/categories/`, `/api/nodes/{node_type}/`) had no
caller and were deleted with the workflow product, along with `CustomNode` and
`handlers/node_loader.py` (`nodes.0006_drop_customnode` drops the table; it held
zero rows).

The app itself is now gone: the two load-bearing files were moved into
`llm/` — see §2b — and the rest was deleted. What survived:

- [`llm/handlers/base.py`](../llm/handlers/base.py) — `BaseNodeHandler`, the
  calling convention every handler implements
- [`llm/handlers/registry.py`](../llm/handlers/registry.py) — `get_registry()`, on
  the agent hot path: `llm/access.py` calls `has_handler()` on **every** LLM
  call and then executes the model through the handler it returns

The graph-structural handlers (core, logic, utility, subworkflow, all 13
triggers) were deleted; see [WORKFLOW_RETIREMENT.md](WORKFLOW_RETIREMENT.md).

---

## 2b. LLM providers & models — app: `llm` — [llm/views.py](../llm/views.py)

Models: `AIProvider`, `AIModel` (tables stay `nodes_aiprovider` / `nodes_aimodel`; the
models moved out of `nodes` state-only, so no table was renamed). Provider vocabulary —
`SUPPORTED_PROVIDERS`, `RETIRED_PROVIDERS`, `PROVIDER_LABELS`, `provider_choices` — lives
in [llm/providers.py](../llm/providers.py) and is the one answer to *which* providers exist.

| URL | Method | What | Access | Complexity | Tested | Serializer | DB tables | Notes |
|-----|--------|------|--------|-----------|--------|-----------|-----------|-------|
| `/api/llm/models/` | GET | List AI providers + models | Auth | O(n) |  | — (hand-built payload) | AIModel, AIProvider, Credential | Filtered to `llm.providers.SUPPORTED_PROVIDERS`, so retired rows left in the table by an un-reseeded instance are not offered. Availability per provider uses `credentials.resolution` (`slugs_for` / `platform_api_key` / `KEYLESS_PROVIDERS`) — the same lookup the executor runs, so what the picker shows is what will actually execute. |
| `/api/nodes/models/` | GET | Legacy alias of the above | Auth | O(n) |  | — | same | Kept for BrowserOS, which ships its own build and cannot be redeployed in lockstep. Both paths are served by `llm.urls` (the `nodes` app that once shadowed `nodes/models/` with `nodes/<str:node_type>/` is gone). |

---

## 3. Compiler — app: `compiler` — **no HTTP surface**

`/api/workflows/{id}/compile/`, `/api/workflows/{id}/validate/` and
`/api/compile/validate/` were deleted along with `compiler/compiler.py`,
`views.py`, `urls.py` and `serializers.py`. No client called them, and the
compiler they wrapped only existed to turn ReactFlow JSON into a LangGraph
graph for the retired DAG runtime.

What remains is library code on the agent hot path:

- [`schemas.py`](../compiler/schemas.py) — `ExecutionContext`, constructed on
  every LLM call (`llm/access.py`) and passed into every node handler

---

## 4. Streaming (SSE) — app: `streaming` — [streaming/views.py](../streaming/views.py)

Model: `StreamEvent`.

| URL | Method | What | Access | Complexity | Tested | Serializer | DB tables | Notes |
|-----|--------|------|--------|-----------|--------|-----------|-----------|-------|
| `/api/streaming/executions/{uuid}/stream/` | GET | Live SSE of execution events | Auth | Stream |  | — | StreamEvent | Long-lived connection |
| `/api/streaming/executions/{uuid}/events/` | GET | Event history for replay | Auth | O(n) |  | `StreamEventSerializer` | StreamEvent | Invalid UUID → HTML 404 (audit) |
| `/api/streaming/status/` | GET | Connection status | Auth | O(1) | ~ | — | — | |
| `/api/streaming/executions/{uuid}/test/` | POST | Fire a test event | Auth (DEBUG) | O(1) | ~ | — | StreamEvent | Debug helper |

---

## 5. Orchestrator — workflows, executions, HITL, AI chat — package: `agents`, app label: `orchestrator` — [agents/views/](../agents/views/)

> The package was renamed to `agents` when the product shifted off the workflow canvas; the app label, the `orchestrator_*` tables, and the `/api/orchestrator/` URL prefix all stayed. Import from `agents.`, but keep writing `orchestrator` in migration dependencies, `to='orchestrator.Workflow'` references, and `reverse('orchestrator:...')`.

Views are split one module per concern; `urls.py` imports the submodules directly,
so the routing table names the owner of each route:
[workflows.py](../agents/views/workflows.py) (CRUD),
[agents.py](../agents/views/agents.py) (agent CRUD, execute, approve, projections),
[hitl.py](../agents/views/hitl.py),
[conversations.py](../agents/views/conversations.py), [system.py](../agents/views/system.py),
[webhooks.py](../agents/views/webhooks.py) (public, unauthenticated — routed from the project URLconf).

The canvas-era modules — `executions.py`, `generation.py`, `partial.py`,
`versions.py` — were deleted along with the workflow canvas they served; no
client called them. `executor.tasks.test_workflow_async` and
`executor/sample_inputs.py` went with the `test/` route.

Models: `Workflow`, `HITLRequest`, `ConversationMessage`. All function-based
views. The DAG-era tables — `WorkflowVersion`, `WorkflowTestResult`,
`WorkflowCloneHistory`, `TriggerState` — were dropped 2026-08-18 by
`orchestrator.0014_drop_dag_era_tables`; `workflow_detail` no longer writes a
version snapshot on PUT.

Agents are `SubAgent` rows — see
[docs/AGENT_TEMPLATES.md](AGENT_TEMPLATES.md) §3 for why. Their routes are in
[agents/views/agents.py](../agents/views/agents.py), not `views.py`.

| URL | Method | What | Access | Complexity | Tested | Serializer | DB tables | Notes |
|-----|--------|------|--------|-----------|--------|-----------|-----------|-------|
| `/api/orchestrator/hitl/pending/` | GET | Pending human-in-the-loop requests | Auth | O(n) |  | `HITLRequestSerializer` | HITLRequest | Security-tested. Joins `execution__subagent`; it selected `execution__workflow` until 2026-08-24 and 500'd on every call — `agents/tests/test_regressions.py::RenamedColumnTests` |
| `/api/orchestrator/hitl/{request_id}/respond/` | POST | Answer a HITL request | Auth | O(1) | ~ | — | HITLRequest | |
| `/api/orchestrator/chat/` `…/{cid}/` `…/{cid}/messages/{mid}/` | GET/POST/DELETE | Builder chat threads | Auth | O(n) |  | — | ConversationMessage, SubAgent | POST persists the user message only; reply generation is not wired up (202, no assistant turn). The body's `workflow_id` (or `subagent_id`) maps to the `subagent` column and is **re-scoped to the caller** — an unowned or unparseable id stores null. It was passed as `workflow_id=` to `.create()` until 2026-08-24 and 500'd on every POST — `agents/tests/test_regressions.py::RenamedColumnTests` |
| `/api/orchestrator/settings/update/` | POST | Update orchestrator settings | Auth | O(1) | ~ | — | UserProfile/settings | |
| `/api/orchestrator/agents/` | GET/POST | List / create agent (a `Workflow` with `kind='agent'`) | Auth | O(n) |  | `AgentSerializer` | Workflow, ExecutionLog, Trigger | `agents/tests/test_agents.py`; stats counted from the log, not stored — `runs`/`unattended` are **distinct** counts (the `hitl_requests` filter forces a LEFT JOIN that multiplied both, and the spend, by the approval count) and `spend` is rupees derived from `tokens_used` via `agents/spend.py`, the same number the spend cap refuses on. A non-blank `schedule` reconciles a `Trigger` row via `AgentSerializer.sync_schedule` **after** save, and is refused unless `allowUnattended` is also on — the runtime rejects every unattended firing otherwise |
| `/api/orchestrator/agents/{id}/` | GET/PUT/PATCH/DELETE | Agent detail | Auth (owner) | O(1) |  | `AgentSerializer` | Workflow, Trigger | PATCH **merges** onto the stored config — a partial save must not reset an unsent grant, `allowUnattended` included. PUT omits nothing: an unsent `allowUnattended` reads as False. `sync_schedule` runs **before** `revisions.record`, so the revision snapshots the schedule as saved rather than the one it replaced |
| `/api/orchestrator/agents/{id}/execute/` | POST | Start an agent run against a goal | Auth (owner) | Heavy |  | `AgentExecuteSerializer` | Workflow, ExecutionLog | **202 + `execution_id`**, run is detached via `background.spawn()`. Guardrails **and the provider credential** are checked *before* responding, so 402 reaches the caller rather than killing a run that looked started — no credential for the agent's `llm_provider` → 402 naming the provider; a provider with no handler → 400. Steps stream to `ws/execution/{id}/` — [agents/agent/stream.py](../agents/agent/stream.py). A `thread_id` naming a paused run **resumes** it on its original `execution_id` rather than opening a second log against the same checkpointer key; an unknown one falls through to a normal start |
| `/api/orchestrator/agents/{id}/approve/` | POST | Approve a paused tool call **and resume the run** | Auth (owner) | Heavy |  | `AgentApproveSerializer` (`thread_id`, `call_id`, `remember`) | ExecutionLog, AgentTurn, AgentStep, ToolPermission | Ownership re-checked: a thread id is not an authorisation. Resumes on the *original* `execution_id` so the trace stays one run |
| `/api/orchestrator/agents/{id}/steer/` | POST | Send a mid-run instruction to a run already going | Auth (owner) | Light |  | `AgentSteerSerializer` (`message`) | SubAgent, ExecutionLog | Lands in the in-process steer mailbox keyed by the run's `thread_id`; the graph's `steering` node picks it up at its next tool boundary. Same run, same log, same stream — no restart. 404 when nothing is running |
| `/api/orchestrator/triggers/` | GET/POST | List / create triggers (schedule, webhook, event) | Auth (owner) | Light |  | `TriggerSerializer` | Trigger, SubAgent | Cron validated by `agents/triggers.py`; creating a schedule arms `next_due_at`. `subagent` ownership checked in `validate_subagent` |
| `/api/orchestrator/triggers/{id}/` | GET/PATCH/DELETE | One trigger | Auth (owner) | Light |  | `TriggerSerializer` | Trigger | Re-enabling clears `consecutive_failures`, else it fires once and self-disables again. `webhook_url` exposes the secret to the owner only |
| `/api/orchestrator/triggers/{id}/run/` | POST | Fire a schedule now, through the sweep's own path | Auth (owner) | Light | `agents/tests/test_triggers.py::RunNowTests` | `TriggerSerializer` | Trigger, ExecutionLog | Calls `sweep.fire`, **not** `start_agent_run` — a manual fire must exercise the overlap policy, the unattended gate and the failure counter, or it proves nothing about the scheduled path. Returns the sweep's one-word `outcome` (`fired`/`busy`/`late`/`skipped`/`refused`/`failed`). Schedule mode only; webhook and disabled triggers answer 400 |
| `/api/orchestrator/hooks/{secret}/` | POST | **Public** webhook receiver — starts an unattended run | **AllowAny** | Heavy |  | — | Trigger, SubAgent, ExecutionLog |  The only unauthenticated route. Secret in the path is the whole credential. Requires `SubAgent.allow_unattended` (enforced again in the runtime, not just here). Body is capped at 64 KB and passed as *context*, never as the goal. Answers 202 with an empty body, and **404 for every refusal** — wrong secret, disabled, and not-cleared must be indistinguishable or it becomes an oracle. A refused or failed firing increments `consecutive_failures` and self-disables at `sweep.MAX_CONSECUTIVE_FAILURES`, like the schedule sweep — until 2026-08-24 only success touched the counter, so a permanently refused hook retried for ever |
| `/api/orchestrator/agents/{id}/reject/` | POST | Decline a paused tool call **and resume the run past it** | Auth (owner) | Heavy |  | `AgentRejectSerializer` (`thread_id`, `call_id`, `reason`) | ExecutionLog, AgentTurn, AgentStep | The mirror of `approve/`, and the fix for an asymmetry that was a bug: approving resumed the run, declining recorded nothing, so a refused call left the run paused for ever. The model is told what was refused (and why, if a `reason` is given) and continues without it. 404 when no paused run holds that thread |

---

## 6. Logs & insights — app: `logs` — [logs/views.py](../logs/views.py), [logs/queries.py](../logs/queries.py)

Models: `ExecutionLog` (one run), `AgentTurn` (one model call), `AgentStep` (one
tool call), `SubAgentRevision` (one configuration). Design note:
[AGENT_OBSERVABILITY.md](AGENT_OBSERVABILITY.md).

A run is a loop of turns, each carrying the model's reasoning and the tool calls
it issued. `AgentStep` was `NodeExecutionLog` until 2026-08-19 (`logs.0013`).

Views are thin sync `@api_view`s; every query lives in `logs/queries.py`.
Responses keep the `workflow_id` / `workflow_name` wire names even though the
column is `subagent` — the frontend and BrowserOS ship their own builds, so the
rename is done in `queries.py` rather than across three repos.

| URL | Method | What | Access | Complexity | Tested | Serializer | DB tables | Notes |
|-----|--------|------|--------|-----------|--------|-----------|-----------|-------|
| `/api/logs/insights/stats/` | GET | Execution statistics | Auth | Aggregate |  | `AnalyticsFilterSerializer` (input only) | ExecutionLog | Scalars live under `summary`, not flat. Carries `by_caller` alongside `by_status` / `by_trigger` |
| `/api/logs/insights/workflow/{id}/` | GET | Per-agent metrics | Auth (owner) | Aggregate |  | — | ExecutionLog, AgentStep | `tool_success_rates` is keyed by **tool name**, not call id. 404 for another user's agent |
| `/api/logs/insights/costs/` | GET | Cost breakdown | Auth | Aggregate |  | `AnalyticsFilterSerializer` (input only) | ExecutionLog, AgentStep | `by_workflow` rows use the same `workflow_id` / `workflow_name` wire names as everywhere else — never the `subagent__*` column keys. `by_tool` replaced `by_node_type` |
| `/api/logs/executions/` | GET | Execution history | Auth | O(limit) |  | `ExecutionListFilterSerializer` (input only) | ExecutionLog | Keyset cursor; `count` only on the uncursored first page. `?caller=` filters chat/orchestrator/trigger/api and 400s on an unknown value |
| `/api/logs/executions/{id}/` | GET | Run detail | Auth (owner) | O(steps + turns) |  | — | ExecutionLog, AgentTurn, AgentStep, SubAgentRevision | Turns nesting their steps, each turn's full reasoning, the revision used, `delegated_by`, and per-step `delegated_runs`. Steps capped at `EXECUTION_NODE_LOG_LIMIT` and turns at `EXECUTION_TURN_LIMIT` (`steps_truncated` / `turns_truncated`); steps with no turn appear under `unattributed_steps`. A malformed UUID is a 404, not a 500 |
| `/api/logs/agents/{id}/revisions/` | GET | Config change timeline | Auth (owner) | O(revisions) |  | — | SubAgentRevision, ExecutionLog | Newest first, with diffs and per-revision `run_count`. Capped at `REVISION_TIMELINE_LIMIT`; the body reports the real `count` and `truncated` so a cut timeline and a short one are distinguishable. 404 for another user's agent |
| `/api/logs/agents/{id}/revisions/{n}/` | GET | One revision's full config | Auth (owner) | O(1) |  | — | SubAgentRevision, ExecutionLog | Full `AgentConfig` snapshot |

Retired 2026-08-19: `/api/logs/audit/`, `/api/logs/audit/export/`,
`/api/logs/executions/{id}/activities/`, `/api/logs/executions/{id}/narrative/`.

Tests: `logs/tests/` — `test_logs.py` (every route), `test_turns.py`,
`test_revisions.py`, `test_delegation.py`, `test_migrations.py`.

---

## 7. Inference / RAG — app: `inference` — [inference/views.py](../inference/views.py)

Models: `KnowledgeBase`, `Document`, `DocumentChunk`, `IndexedTerm`. Retrieval
backends live in `inference/backends/` — a KB's `backend` field (`vector` /
`fulltext` / `raw` / `hybrid`) picks the machinery; ingestion and deletion fan
out through it. Vector search stays FAISS HNSW; keyword search is our own
inverted index (DB-agnostic across SQLite/Postgres). `KnowledgeBase` is
**internal** — one implicit Default per user, auto-created on first upload;
there is deliberately **no HTTP CRUD** for KBs (`/api/inference/kbs/` was
removed to prevent orphaning indexed state and cross-tenant attachment).

**The per-user file system** (`Folder`, 2026-08-25) is orthogonal to all of
that: a folder organises, a KB indexes, and moving a file between folders
touches no vectors — `inference/filesystem.py` does not import
`inference/tasks.py`, so the move path *cannot* re-index, and a test enforces
it. Three properties are load-bearing:

- **Root is `NULL`, not a row.** `folder_id IS NULL` is the user's root. NULL is
  unforgeable, so the most-used location is not an id anyone can get wrong, and
  every pre-existing `Document` was correctly placed the moment the column
  appeared — no backfill, no lazy root creation.
- **The API is id-addressed, never path-addressed.** `path` and `folder_path`
  go out for display; no route accepts either as a locator. `Folder.path` holds
  *ids* (`/12/45/`), which makes rename O(1), cycle detection a string compare,
  and a subtree one indexed prefix match.
- **One choke point.** Every inbound folder id resolves through
  `filesystem.resolve_folder`, which raises the same error for unknown and
  foreign ids alike -> **404 for both**. A 403 would be an ownership oracle.
  `test_filesystem.py::ChokePointTests` fails if `Folder.objects` is used
  outside the modules allowed to.

**Trash is a state, not a place.** `deleted_at` plus a filtered default manager
(`LiveManager`) means a trashed row leaves every listing in the codebase —
including `chat/tools/knowledge.py` and `kb.documents` — without any of them
being edited. Deleting drops the vector index *immediately* (a file the user
cannot see must not keep answering RAG queries); the row itself survives for
`RECYCLE_BIN_RETENTION_DAYS` (30) and is then purged by
`inference.sweep_recycle_bin` / `manage.py purge_recycle_bin`.

| URL | Method | What | Access | Complexity | Tested | Serializer | DB tables | Notes |
|-----|--------|------|--------|-----------|--------|-----------|-----------|-------|
| `/api/inference/folders/` | GET | Children of `?parent=<id>` (absent = root), with breadcrumbs | Auth (owner) | O(n) | OK | `FolderSerializer` | Folder, Document | Capped at `FOLDER_CHILDREN_LIMIT` (500) with `truncated` in the body — no cursor, folder rows are tiny. Child/document counts are annotated and must spell out the `deleted_at` filter, because `Count` follows the raw relation rather than the default manager. Foreign/unknown parent gives 404 |
| `/api/inference/folders/` | POST | Create a folder | Auth (owner) | O(1) | OK | `FolderWriteSerializer` | Folder | `{name, parent_id}`. Duplicate sibling name gives 400 — rejected, not auto-suffixed, because the name is a deliberate choice. Two partial unique constraints, since SQL treats NULLs as distinct and the `(user, parent, name)` one does not reach top-level folders at all |
| `/api/inference/folders/{id}/` | GET/PATCH/DELETE | Read; rename and/or move; send to the bin | Auth (owner) | O(1) | OK | `FolderSerializer` | Folder, Document | PATCH takes `name` and/or `parent_id` in one call. Moving into self or a descendant gives 400 — the cycle check is `target.path.startswith(folder.path)`, no queries. DELETE trashes the whole subtree and answers 200 with `purges_after_days`, not 204: the client needs to say how long it stays restorable |
| `/api/inference/fs/move/` | POST | Reparent folders and/or documents in bulk | Auth (owner) | O(subtree) | OK | `MoveSerializer` | Folder, Document | `{folder_ids[], document_ids[], target_folder_id}`, capped at `MAX_MOVE_BATCH` (200). Bulk because multi-select drag would otherwise be N requests and N chances to half-apply. Descendant paths are rewritten in one `UPDATE` via `Replace()`; the descendants must be identified *before* the parent is saved, or the rewrite silently matches nothing |
| `/api/inference/trash/` | GET | What the caller can still restore | Auth (owner) | O(n) | OK | `FolderSerializer` / `DocumentListSerializer` | Folder, Document | Only `trashed_directly` rows — a subtree delete is one entry, not one per node. Carries `deleted_at`, `purges_at` and `purges_after_days`; read that last one rather than hardcoding 30 in a client, it is an env var |
| `/api/inference/trash/restore/` | POST | Put trashed rows back | Auth (owner) | O(subtree) | OK | `RestoreSerializer` | Folder, Document | **Has no target parameter** — a restore goes to the row's own recorded parent, so "restore into someone else's folder" is not an attack to guard but a request that cannot be expressed. Per-item outcomes (`restored[]` / `refused[]`), never a bare boolean. A still-trashed ancestor gives `parent_still_trashed`; a name taken since the delete auto-suffixes and reports `renamed_to`. Restored documents are re-ingested through the ordinary upload door |
| `/api/inference/trash/empty/` | DELETE | Purge the caller's bin now | Auth (owner) | Heavy | OK | — | Folder, Document, DocumentChunk, IndexedTerm | The sweep with retention 0, scoped to `request.user` |
| `/api/inference/documents/` | GET/POST | List/upload documents | Auth | Heavy | ✅ | `DocumentSerializer` | Document, DocumentChunk, IndexedTerm, Folder | Ingestion branches on KB backend: vector/hybrid embed, fulltext chunks only, raw stores text with status `stored`. `file_type` comes from `utils.normalize_file_type(name, mime)` — the one vocabulary, shared with `chat/sources/attachments.py`; never the raw extension. GET takes an optional `folder_id=<id>` or `folder_id=root`; **absent keeps the flat listing**, which is what leaves every existing client working. POST takes `folder_id` in the multipart body (absent = root). A foreign folder gives 404. Uncursored GET capped at 50/list with `truncated`, both lists ordered `-created_at` like the cursor branch; `DocumentSerializer.content` is the full text, so page it. Files are stored at `users/<user_id>/<uuid><ext>` — every segment server-derived, so the physical layout carries no user input and the tree is **not** mirrored on disk |
| `/api/inference/documents/{id}/` | GET/DELETE | Document detail | Auth (owner) | O(1) | ✅ | `DocumentSerializer` | Document, KnowledgeBase, Folder | **DELETE now trashes rather than deletes** — 200 with `purges_after_days`, not 204. It drops the vector index at once (`remove_document_from_kb` + `refresh_kb_stats`) but keeps `content_text` and the file, so restore is a re-ingest through the upload door. `post_delete` does *not* fire on a trash, so the trash path calls the extracted `signals.recount_kb` for `doc_count`; the signal still covers permanent deletes and the two `chat/sources/attachments.py` cleanup paths |
| `/api/inference/documents/{id}/share/` | POST | Share document | Auth | O(1) |  | — | Document | Platform KB stays vector-only by convention. The row is saved **before** the worker thread starts — it re-reads the row for its metadata, so spawning first was a race it could lose. Re-sharing → 403 |
| `/api/inference/documents/{id}/download/` | GET | Download original | Auth (owner) | O(1) | ✅ | — | Document | Streams only if the stored path resolves inside `MEDIA_ROOT` (`views._servable`, reusing `llm/handlers/openai_compatible.validate_attachment_path`), closing the traversal gap the 2026-08-24 audit found. Otherwise it falls back to the extracted text |
| `/api/inference/rag/search/` | POST | Vector search | Auth | Heavy |  | `RagSearchSerializer` | DocumentChunk | FAISS. A KB that cannot be opened answers **503** (`KnowledgeBaseUnavailable`), never an empty `results` list — a broken embedder and an empty corpus must not look alike |
| `/api/inference/rag/query/` | POST | RAG answer (search + LLM) | Auth | External |  | `RagQuerySerializer` | DocumentChunk | `get_rag_pipeline` is **async** — the sync version awaited from this async view raised `SynchronousOnlyOperation`, was swallowed, and fell back to `get_hnsw_kb(-user_id)`, i.e. the platform KB for user 1 and the skills index for user 2. Nothing may derive a KB id from a user id. 503 on an unopenable KB |

Chat tools for retrieval are in `chat/tools/knowledge.py`:
`list_knowledge_bases` (reports each KB's backend),
`knowledge_base_search` (semantic; reroutes on fulltext/raw KBs with advice),
`keyword_search` (exact/prefix + quoted phrases), `list_documents`,
`read_document` (12k-char windows). Misroutes return routing advice, not
errors — see `chat/tests/test_knowledge_tools.py`.

### 7b. Extraction — merged into `inference` 2026-08-18 — [inference/extraction_views.py](../inference/extraction_views.py)

The retired `extraction` app (routes, serializers, review semantics) folded into
`inference`; the `/api/extraction/` path is kept as a stable alias. Models
`ExtractionSchema`, `ExtractedRow` live in `inference/models.py` (tables
`inference_extractionschema` / `inference_extractedrow`); the engine lives in
`inference/extraction.py` — one `chat.turn.llm.complete()` call per document
(min-field confidence decides hold-vs-accept), dispatch mirrors the agent-run
split (`RUN_WORKFLOWS_ASYNC` → Celery 202 / sync inline), plus the
`manage.py run_extraction` command. Re-running replaces accepted/needs_review
rows; a `reviewed`/`rejected` row is a human decision and is never overwritten.

| URL | Method | What | Access | Complexity | Tested | Serializer | DB tables | Notes |
|-----|--------|------|--------|-----------|--------|-----------|-----------|-------|
| `/api/extraction/schemas/` | GET/POST | List/create schemas | Auth (owner) | O(n) |  | `ExtractionSchemaSerializer` | ExtractionSchema | list is paginated (`results`); duplicate field names and unknown field types rejected |
| `/api/extraction/schemas/{id}/` | GET/PATCH/DELETE | Schema detail | Auth (owner) | O(1) |  | `ExtractionSchemaSerializer` | ExtractionSchema | threshold PATCH re-sorts existing rows (`apply_threshold`), never touching `reviewed`/`rejected` |
| `/api/extraction/schemas/{id}/rows/` | GET/POST | List rows (status filter) / add rows | Auth (owner) | O(n) |  | `ExtractedRowSerializer` | ExtractedRow | POST applies the threshold before returning |
| `/api/extraction/schemas/{id}/extract/` | POST | Run LLM extraction over `{document_ids}` (≤100) | Auth (owner) | External |  | — | ExtractionSchema, ExtractedRow, Document | `RUN_WORKFLOWS_ASYNC` → 202 `{async: true, task_id}`; else 200 `{async: false, processed, created, needs_review, held_decided, errors}`; foreign/missing doc ids → 400 |
| `/api/extraction/rows/` | GET | Rows across the caller's schemas | Auth (owner) | O(n) |  | `ExtractedRowSerializer` | ExtractedRow | `status` and `schema` query params; `schema_name` included for cross-schema queues. **No POST** (405): the serializer carries no `schema`, so a root create reached the DB with a null FK — rows are created via `schemas/{id}/rows/` |
| `/api/extraction/rows/{id}/review/` | POST | Accept/correct/reject a held row | Auth (owner) | O(1) |  | `ExtractedRowSerializer` | ExtractedRow | `{data?: corrections, reject?: true}`; 400 on fields not on the schema; response has `corrected` |
| `/api/extraction/rows/{id}/` | GET/PATCH/PUT/DELETE | Read, correct or drop one row | Auth (owner) | O(1) |  | `ExtractedRowSerializer` | ExtractedRow, Document | Not a full ModelViewSet — create is deliberately absent. `document` is a writable FK scoped by `validate_document` to the caller's own documents; unscoped, a user could point their row at anyone's document and the review audit trail became unverifiable |

---

## 8. Credentials — app: `credentials` — [credentials/views.py](../credentials/views.py)

Models: `CredentialType`, `Credential` (encrypted), `CredentialAuditLog`. Router-based
ViewSets. **IDOR-sensitive — verify per-user scoping when reviewing.**

| URL | Method | What | Access | Complexity | Tested | Serializer | DB tables | Notes |
|-----|--------|------|--------|-----------|--------|-----------|-----------|-------|
| `/api/credentials/` | GET/POST | List/create credentials | Auth (owner) | O(1) |  | `CredentialSerializer` | Credential, CredentialType | `credential_type` accepts PK **or** slug (active types only); write-only `data` dict split into `public_metadata` / encrypted blob by the type's `fields_schema` (`public: true` ⇒ plaintext); secrets encrypted at rest. Create validates `data` against `fields_schema` (`credential.manager.validate_against_schema`) — a credential missing required fields is now a 400, not a runtime failure. Duplicate `user+name` is a 400 (IntegrityError caught), not a 500. Create/update/delete bust the `CredentialManager` cache. List is wrapped: `{credentials: [...]}` |
| `/api/credentials/{pk}/` | GET/PUT/PATCH/DELETE | Credential CRUD | Auth (owner) | O(1) |  | `CredentialSerializer` | Credential | Adversarial-tested. Read masks secrets (`********` + last 4) so writes **merge** `data` over stored values — omitted keys keep their value; update resets `is_verified` only when `data` changes; DELETE 400s if an active agent (`SubAgent.llm_credential`) uses it, and writes a `deleted` audit row carrying a `snapshot` (name/type) because the FK is nulled by SET_NULL |
| `/api/credentials/types/` | GET | Credential-type registry | Auth | O(n) | ~ | `CredentialTypeSerializer` | CredentialType | **Read-only** (POST/DELETE ⇒ 405); rows are seeded by `credentials.0005_seed_credential_types` (so `migrate` alone is sufficient for a fresh install) and re-seedable with `manage.py seed_connector_credentials`, which remains the single source of truth for the data the migration imports. Both set `service_identifier = slug` — the key the frontend node configs match on. Wrapped: `{types: [...]}`.  Tests must use `update_or_create` when making a `CredentialType`: the table is no longer empty at test start |
| `/api/credentials/oauth/google/init/` | GET | Returns provider auth URL (signed `state`, 10 min) | Auth | External |  | `CredentialOAuthInitSerializer` | — | `redirect_uri` must match `ALLOWED_REDIRECT_ORIGINS`; there is no `/authorize/` redirect route |
| `/api/credentials/oauth/google/callback/` | POST | Exchange code → tokens, create/update credential | Auth | External |  | `CredentialOAuthCallbackSerializer` | Credential | Requires the `google-oauth2` type to be seeded; **`state` is mandatory** (400 when missing, signed, 10-min expiry, bound to user + redirect_uri). Re-connecting the same account updates the existing `Google Account` credential (update-or-create by `user+name`) instead of 500ing on the unique constraint. OAuth refresh is Google-only by design: `Credential.get_valid_access_token` and `CredentialManager.refresh_oauth_token` refuse non-`google-oauth2` types and exchange with the settings client. Called by the frontend `/oauth/callback` popup page (`pages/OAuthCallback.tsx`), which posts `OAUTH_SUCCESS`/`OAUTH_ERROR` back to its opener. Tokens land in the `access_token`/`refresh_token` columns, which `mcp_integration`'s injector reads as a fallback |
| `/api/credentials/logs/` | GET | Audit logs | Auth | O(n) | ~ | `CredentialAuditLogSerializer` | CredentialAuditLog | Route 404s in audit (§7) — verify. `credential_name`/`credential_type_name` fall back to the row's `snapshot` when the credential was deleted (`SET_NULL`), so deletion history stays readable |

---

## 9. Templates (community) — app: `templates` — [templates/views.py](../templates/views.py)

Models: `WorkflowTemplate`, `WorkflowRating`, `WorkflowBookmark`, `TemplateComment`.

| URL | Method | What | Access | Complexity | Tested | Serializer | DB tables | Notes |
|-----|--------|------|--------|-----------|--------|-----------|-----------|-------|

---

## 10. MCP integration — app: `mcp_integration` — [mcp_integration/views.py](../mcp_integration/views.py)

Models: `MCPServer`, `MCPServerPreference`. Router ViewSet. **Servers can execute code — security-sensitive.**

Backs the frontend **Connections** page (`/connections`), which merged the former
`/connectors` and `/mcp-servers` pages — both now redirect there.

Ownership rule: `user IS NULL` rows are shared curated templates. Their *config* is
read-only (403 via `_assert_owner`), but any user may enable/disable one for
themselves — that choice is a `MCPServerPreference` row, and `effective_enabled`
on the serializer is the value a UI should render. `enabled` is the shared flag.

| URL | Method | What | Access | Complexity | Tested | Serializer | DB tables | Notes |
|-----|--------|------|--------|-----------|--------|-----------|-----------|-------|
| `/api/mcp/servers/` | GET/POST | List/create MCP servers | Auth (owner) | O(1) |  | `MCPServerSerializer` | MCPServer, MCPServerPreference | `tests_services` (73). Serves presentation metadata (`label`, `category`, `tagline`, `icon_slug`, `help_url`) so the catalog is data, not frontend code. One extra query resolves `effective_enabled` for the whole page via `disabled_server_ids` in serializer context |
| `/api/mcp/servers/{pk}/` | GET/PUT/PATCH/DELETE | Server CRUD | Auth (owner) | O(1) |  | `MCPServerSerializer` | MCPServer, MCPServerPreference | A PATCH whose body is **exactly** `{enabled}` on a system server writes a preference and returns 200; any other field on a system server ⇒ 403, including `{enabled, command}` together. `tests_connections` |
| `/api/mcp/servers/{pk}/set-enabled/` | POST | Turn a connection on/off for the current user | Auth | O(1) |  | `MCPServerSerializer` | MCPServer, MCPServerPreference | Preferred over PATCH; works uniformly for system (preference row) and owned (row's own `enabled`) servers, so the UI needs one control. Invalidates `MCPToolCache`. Non-boolean ⇒ 400. `tests_connections` |
| `/api/mcp/servers/{pk}/tools/` | GET | Capabilities of one server | Auth | External | ✓ | — | MCPServer | `LIST_TOOLS_TIMEOUT` (30s) — `npx -y` installs before the server prints a byte (~8.5s measured), so the old 5s budget timed out on *healthy* connectors too. Powers the "What it can do" disclosure, so it deliberately ignores the enable toggle: listing is how a user decides whether to turn a connection on. Every failure carries a `code` and a non-empty `error`: `credential_missing`/`credential_invalid` ⇒ 400, lost access ⇒ 403, `connection_timeout` ⇒ 504, `connection_failed` ⇒ 502 (message includes the child's stderr, e.g. npm's 404). `tests_tool_discovery` |
| `/api/mcp/servers/{pk}/validate_credentials/` | GET | Dry-run credential resolution | Auth | O(1) | ~ | — | MCPServer, Credential | Returns `{ok, errors}`; a resolver that throws is reported as `{ok: false, errors: [reason]}` rather than 500 — a diagnostic that crashes diagnoses nothing |
| `/api/mcp/servers/tools/` | GET | Aggregate tools from all servers | Auth | External | ~ | — | MCPServer | Per-server `LIST_TOOLS_TIMEOUT`, queried concurrently (`get_all_tools_from_all_servers`); one failing connector degrades to an empty list for that server rather than emptying the response. Unused by any frontend (the Connections page fetches per-server tools) |

**Credential mapping sources** (`credential_env_map` / `credential_header_map`): a value
is normally `"<credential_slug>:<field>"`, resolved from the user's vault. The sentinel
slug `@settings` (e.g. `"@settings:GOOGLE_OAUTH_CLIENT_ID"`) reads from Django settings
instead — used for platform-owned values such as the Google OAuth client, so users are
not asked to create their own. Field lookup also falls back to a Credential's
`access_token`/`refresh_token` columns, which is where the OAuth flow stores tokens.
See `tests_credential_bridge`, whose `CuratedCatalogIntegrityTests` fails if any curated
mapping names a field its credential type does not define.

---

## 11. Skills — app: `skills` — [skills/views.py](../skills/views.py)

Model: `Skill`. Router ViewSet.

| URL | Method | What | Access | Complexity | Tested | Serializer | DB tables | Notes |
|-----|--------|------|--------|-----------|--------|-----------|-----------|-------|
| `/api/skills/` | GET/POST | List/create skills | Auth | O(1) | ~ | `SkillSerializer` | Skill | |
| `/api/skills/{pk}/` | GET/PUT/PATCH/DELETE | Skill CRUD | Auth (owner/admin write; all read) | O(1) |  | `SkillSerializer` | Skill | Edit/delete owner-or-admin; others read and fork. Content capped at 10,000 words |
| `/api/skills/search/` | GET | Hybrid search (`query`, `tab`, `category`, `page`, `page_size`) | Auth | O(log N) |  | `SkillSearchSerializer` | Skill | FAISS ANN over the shared skills KB (`inference.engine.get_skills_knowledge_base`, id=-2); fuzzy-text fallback when the embedder is unavailable |

---

## 12. Chat (standalone agent + guest) — app: `chat` — [chat/views.py](../chat/views.py), [chat/guest/views.py](../chat/guest/views.py)

Models: `ChatSession`, `ChatMessage`, `ChatAttachment`. Native tool-calling agent
([chat/turn/agent.py](../chat/turn/agent.py)) with a `SENSITIVE_TOOLS` HITL gate. Both send
endpoints run the same pipeline ([chat/turn/pipeline.py](../chat/turn/pipeline.py)) and
differ only in their event sink, so their behaviour cannot drift.

| URL | Method | What | Access | Complexity | Tested | Serializer | DB tables | Notes |
|-----|--------|------|--------|-----------|--------|-----------|-----------|-------|
| `/api/chat/sessions/` | GET/POST | List/create chat session | Auth | O(1) |  | `ChatSessionSerializer` | ChatSession | Blank title accepted (audit gap 2) |
| `/api/chat/sessions/{pk}/` | GET/PUT/PATCH/DELETE | Session CRUD | Auth (owner) | O(1) |  | `ChatSessionSerializer` | ChatSession, ChatMessage | |
| `/api/chat/sessions/{sid}/message/` | POST | Send message, wait for full reply | Auth | External |  | — | ChatMessage, ChatSession | Same pipeline as the stream, events discarded |
| `/api/chat/sessions/{sid}/message/steer/` | POST | Say something to a chat turn already running | Auth (owner) | Light |  | — (JSON `message`) | — (in-process mailbox) | Not a second turn: two turns on one session interleave into one transcript. 404 when no turn is running, so "delivered" and "went nowhere" cannot look alike |
| `/api/chat/sessions/{sid}/message/stream/` | POST | Send message (SSE stream) | Auth | Stream/External |  | — | ChatMessage, ChatSession, ToolOutput, ToolPermission | Body also takes `approve_tool_call` and `remember_approval` (the latter stores a standing allowance for that tool — see `chat/tools/permissions.py`). Streams `content_chunk` token-by-token; auth is manual (DRF cannot wrap `StreamingHttpResponse`). The turn is owned by `chat/turn/runs.py`, **not** by this response — dropping the connection does not cancel it. A send while the session is already answering attaches to the running turn instead of starting a second one |
| `/api/chat/sessions/{sid}/message/attach/` | POST | Re-attach to a running turn (SSE) | Auth (run owner) | Stream |  | — | — | Replays every buffered frame, then follows live; body `{from: n}` skips frames the caller has. Closes with no frames when no run exists (answer is already in the DB). Read-only: never starts work |
| `/api/chat/sessions/{sid}/message/stop/` | POST | Stop the running turn | Auth (run owner) | O(1) |  | — | ChatMessage | The **only** thing that cancels a turn. Persists whatever streamed as an answer with `metadata.interrupted`; emits a closing `done` to everyone attached. 404 when nothing is running |
| `/api/chat/runs/` | GET | Session ids with a turn still running | Auth | O(n) in live runs |  | — | — | In-memory, scoped to the caller. Drives re-attach after a reload and the "still working" marker. Per-process — see the multi-worker note in `chat/turn/runs.py` |
| `/api/chat/sessions/{sid}/messages/{mid}/` | DELETE | Delete a message | Auth (owner) | O(1) | ~ | — | ChatMessage | |
| `/api/chat/sessions/{sid}/upload/` | POST | Upload attachment | Auth | O(1) | ~ | — | ChatAttachment | Multipart |
| `/api/chat/execute-tool/` | POST | Execute a chat tool | Auth | External |  | — | MCPServer, ToolPermission | Non-object `args` → 400; **403 whenever the agent loop would have paused** — `SENSITIVE_TOOLS` *or* `chat.permissions.default_policy`, so a credentialed MCP write cannot be run here to dodge the gate |
| `/api/chat/guest/sessions/` | POST | Create guest session | **Public** (IP-limited) | O(1) | ~ | — | ChatSession | Pinned to NVIDIA NIM + `nvidia/nemotron-3.5-lightning-30b-a3b`; a requested provider/model is ignored |
| `/api/chat/guest/sessions/{sid}/` | GET | Get guest session | **Public** | O(1) | ~ | — | ChatSession, ChatMessage | |
| `/api/chat/guest/sessions/{sid}/message/stream/` | POST | Guest stream message | **Public** (IP-limited) | Stream/External | ~ | — | ChatMessage | Re-pins the session row to the guest provider/model before answering |

---

## 13-14. Buddy + BrowserOS — **REMOVED 2026-08-16**

`api/buddy/` and `api/browseros/` are gone, along with the `ws/buddy/` socket.
They were one subsystem: `buddy/views.py` drove `browserOS`'s `OSWorkspace` /
`OSAppWindow` models, and the BrowserOS frontend's only backend call was
`POST /api/buddy/commands/`. BrowserOS now runs disconnected. Chat lost the
screen-context block that `ws/buddy/` warmed; the `screen_context` field the
chat POST body carried was never read by anything.

---

## 15. Canvas — **REMOVED**

Two separate things, both gone.

**The canvas agent** (`canvas_agent`, "Platform Copilot") — removed 2026-08-14:
the Django app, its routes, its WebSocket consumer, and `docs/CANVAS_AGENT.md`.
Its frontend was kept at the time for a later feature; that never came, and the
files (`hooks/useCanvasAgent.ts`, `components/workflow/CanvasAgentBar.tsx`,
`contexts/CanvasAgentContext.tsx`) are no longer present either.

**The agent canvas** (`/agents/:id/canvas`, the run-debugging graph) — removed
2026-08-24. Gone with it: `agents/agent/graph_projection.py`,
`agents/views/canvas.py`, `agents/tests/test_agent_canvas.py`, and the three
endpoints they served (`agents/{id}/graph/`, `agents/{id}/runs/`,
`executions/{eid}/graph/`). On the frontend: `pages/AgentCanvas.tsx`, the whole
`components/workflow/` directory, `lib/executionEvents.ts` and its test, and the
`reactflow` dependency, which had no other consumer. `/agents/:id/canvas` now
redirects to `/agents`.

A run is read on `/runs`, in the Inbox, and through the `/api/logs/` endpoints —
`ExecutionLog` → `AgentTurn` → `AgentStep` is still written exactly as before.
Nothing about how runs are *recorded* changed; only the graph view of them is
gone.

---

## 16. Notifications — app: `notifications` — [notifications/views.py](../notifications/views.py)

Models: `Notification`, `NotificationPreference`, `HITLReminderSchedule`. Router
ViewSet (full CRUD) plus two non-router routes declared *before* the router — its
`''` registration is greedy and would otherwise capture them as detail lookups.

| URL | Method | What | Access | Complexity | Tested | Serializer | DB tables | Notes |
|-----|--------|------|--------|-----------|--------|-----------|-----------|-------|
| `/api/notifications/` | GET/POST | List/create notification | Auth | O(1) | ~ | `NotificationSerializer` | Notification |  POST → IntegrityError 500 (audit Bug 3) — `perform_create` misses `user` |
| `/api/notifications/{pk}/` | GET/PUT/PATCH/DELETE | Notification CRUD | Auth (owner) | O(1) | ~ | `NotificationSerializer` | Notification | |
| `/api/notifications/{pk}/mark_read/` (action) | POST | Mark read | Auth (owner) | O(1) | ~ | — | Notification | |
| `/api/notifications/mark_all_read/` (action) | POST | Bulk mark read | Auth (owner) | O(n) | ~ | — | Notification | |
| `/api/notifications/preferences/` | GET/PUT/PATCH | HITL reminder delivery rules | Auth (self) | O(1) |  | `NotificationPreferenceSerializer` | NotificationPreference | get_or_create on read; `last_digest_sent_on`/`last_hourly_sent_at` are read-only — they are the once-per-day email cap |
| `/api/notifications/hitl-reminders/` | GET | Caller's armed escalation ladders | Auth (owner) | O(n) | ~ | `HITLReminderScheduleSerializer` | HITLReminderSchedule, HITLRequest | Read-only; diagnostics for "why did/didn't I get nudged" |

**Reminder delivery** (`notifications/reminders.py`, swept by
`notifications.sweep_hitl_reminders` on Celery beat every
`HITL_REMINDER_SWEEP_SECONDS`, default 300, or by
`manage.py send_hitl_reminders`):

| Channel | Trigger | Transport |
|---------|---------|-----------|
| Escalation | +0 / +1h / +1d after an unanswered `HITLRequest`, then stops | Device push (`ws/hitl/`) + in-app row. **Never email.** |
| Hourly | Opt-in; once an hour while anything is pending | Device push + in-app row. **Never email.** |
| Daily digest | User's chosen local wall-clock time | Email + in-app row + device push. Capped at one per calendar day. |

---

## 17. Imagine (media generation) — app: `imagine` — [imagine/views.py](../imagine/views.py)

Models: `Generation`, `ImagineConversation`, `ImagineMessage`. Router ViewSets + agent APIViews.

| URL | Method | What | Access | Complexity | Tested | Serializer | DB tables | Notes |
|-----|--------|------|--------|-----------|--------|-----------|-----------|-------|
| `/api/imagine/capabilities/` | GET | Model catalog per modality + `defaults`/`recommended` | Auth | External (cached 1h) |  tests_api | — | — | `?refresh=1` bypasses the cache (serialised by a lock so concurrent refreshes cannot stampede; the loser serves the cached copy). 400 + `detail` when the user has no OpenRouter credential. Image/video come from OpenRouter's `/images/models` and `/videos/models`; audio is curated in `services/catalog.TTS_MODELS` (no TTS discovery endpoint exists) |
| `/api/imagine/agent/chat/` | POST | Media-gen agent chat | Auth | External | — | — | ImagineConversation, ImagineMessage | Optional `model` pins the model **and** the modality (see `intent.classify(preferred_model=…)`). Carries the `imagine_generate` cost throttle |
| `/api/imagine/agent/resume/` | POST | Resume agent run (after HITL) | Auth | External | — | — | ImagineConversation | Carries the `imagine_generate` cost throttle |
| `/api/imagine/conversations/` | GET | Conversation list | Auth (owner) | O(1) | — | `ImagineConversationSerializer` | ImagineConversation, ImagineMessage | Read-only viewset despite the router's default verbs |
| `/api/imagine/conversations/{pk}/` | GET | Conversation detail + messages | Auth (owner) | O(n messages) | — | `ImagineConversationDetailSerializer` | ImagineConversation, ImagineMessage | |
| `/api/imagine/` | GET/POST | Generation list/create | Auth (owner) | External |  tests_api | `GenerationSerializer` | Generation | POST create is preflighted: no OpenRouter credential → 400 + `detail`, no row created. Image dispatch is async only under `RUN_WORKFLOWS_ASYNC` (row returns `pending`, worker broadcasts completion; falls back to inline if the broker is unreachable), otherwise image/audio block in the request cycle (up to 120 s); video always returns `pending` and is polled by Celery. Create carries the `imagine_generate` cost throttle. `validate()` rejects a model the modality cannot run. `metadata` is read-only |
| `/api/imagine/{pk}/` | GET/PUT/PATCH/DELETE | Generation detail | Auth (owner) | O(1) |  tests_api | `GenerationSerializer` | Generation | |

---

## 18. Evals + Tuning — **REMOVED 2026-08-16**

`api/evals/` and `api/tuning/` were already routed out; the apps are now
deleted (`AGENT_BLOCKS_PLAN.md` §6). Both accepted work, wrote a `queued` row
and had no executor behind them.

`tuning/` went in that pass; `evals/` did not, and survived un-routed and out of
`INSTALLED_APPS` until 2026-08-17, when the package was deleted for real. Dev
databases predating that still carry four empty `evals_*` tables and an
`evals.0001_initial` row in `django_migrations` — inert, since Django ignores
tables for uninstalled apps, but safe to drop.

**Superseded 2026-08-24 by `eval/` (§20)** — a different app, singular, with
tables `eval_*` precisely so a fresh `migrate` cannot collide with the rows
above. It has an executor behind it (`eval/runner.py` sweeps through
`agents.agent.runtime.run_agent`), which is the thing the old one never had.

## 19. Datasets — **REMOVED 2026-08-18**

`datasets/` is gone too: it was the storage half of the improve loop (the
capture side — nothing ever wrote rows from a correction — and the consume side
— evals/tuning, deleted above — were both absent), so it interacted with nothing
at runtime. Dev databases predating this carry two empty `datasets_*` tables
(`datasets_dataset`, `datasets_datasetrow`) and a `datasets.0001_initial` row in
`django_migrations` — safe to drop.

---

## 20. Eval — sub-agent evaluation + human supervision — app: `eval` — [eval/views.py](../eval/views.py)

Models: `EvalSuite` (cases + supervision policy), `EvalCase` (one goal + its
graders), `EvalRun` (one sweep, pinned to a `SubAgentRevision`), `EvalResult`
(one case, pointing at the `ExecutionLog` it produced), `EvalReview` (a person's
verdict). Design note: [EVALUATION.md](EVALUATION.md).

Views are thin sync `@api_view`s; reads live in `eval/queries.py`. Only
`suite_run` is async (adrf), because it preflights the provider before
answering. Every route is scoped to `request.user` through the query layer — a
suite, case, run or result belonging to someone else is a **404**, never a 403.

A sweep runs the agent once per case through the same `run_agent` door as every
other run, so a suite's size is a bill: `EVAL_MAX_CASES_PER_SUITE` (200) and
`EVAL_MAX_CONCURRENCY` (4) are the bounds, and list responses carry their own
caps (`EVAL_RUN_LIST_LIMIT`, `EVAL_RESULT_LIST_LIMIT`, `EVAL_REVIEW_QUEUE_LIMIT`)
because DRF pagination never reaches `@api_view` functions.

| URL | Method | What | Access | Complexity | Tested | Serializer | DB tables | Notes |
|-----|--------|------|--------|-----------|--------|-----------|-----------|-------|
| `/api/eval/graders/` | GET | Grader catalog | Auth | O(1) |  | — | — | Served from `graders.REGISTRY`, the same dict the runner dispatches through — a picker can never offer a grader nothing implements |
| `/api/eval/suites/` | GET/POST | List / create suites | Auth (owner) | O(suites) |  | `EvalSuiteSerializer` | EvalSuite, EvalCase, EvalRun | GET also returns `health` (per-suite case/run/pending-review counts). POST 400s on another user's `subagent`, an unknown `supervision`, `pass_threshold` outside [0,1], or `concurrency` above the cap |
| `/api/eval/suites/{id}/` | GET/PATCH/DELETE | One suite (+ its cases) | Auth (owner) | O(cases) |  | `EvalSuiteSerializer`, `EvalCaseSerializer` | EvalSuite, EvalCase | 404 for another user's suite |
| `/api/eval/suites/{id}/cases/` | GET/POST | List / add cases | Auth (owner) | O(cases) |  | `EvalCaseSerializer` | EvalCase | **Graders validated on write** against `graders.REGISTRY`: unknown type, missing required param, unknown param or non-positive weight → 400. Capped at `EVAL_MAX_CASES_PER_SUITE` |
| `/api/eval/cases/{id}/` | GET/PATCH/DELETE | One case | Auth (owner) | O(1) |  | `EvalCaseSerializer` | EvalCase | DELETE keeps the results that scored it — `EvalResult.case` is SET_NULL with `case_name`/`goal` copied |
| `/api/eval/suites/{id}/run/` | POST | Sweep the suite | Auth (owner) | External × cases |  | `RunRequestSerializer` | EvalRun, EvalResult, ExecutionLog | **202 + `run_id`**, detached via `background.spawn()`. Guardrails + provider preflight happen first: spent cap or missing credential → **402**, retired model/unknown provider → **400**, no active cases → 400, no agent named → 400. `agent_id` falls back to the suite's `subagent`; another user's agent is a 404 |
| `/api/eval/runs/` | GET | Sweep history | Auth (owner) | O(limit) |  | `RunListFilterSerializer` (input), `EvalRunSerializer` | EvalRun | `?suite_id`/`?agent_id`/`?status`; unknown status → 400. Body carries `count` + `truncated` |
| `/api/eval/runs/{run_id}/` | GET | One sweep + every result | Auth (owner) | O(results) |  | `EvalRunSerializer`, `EvalResultSerializer` | EvalRun, EvalResult, EvalReview, ExecutionLog | Each result carries its `grades`, any `review`, `final_passed`/`final_score`, and `execution_id` for `/api/logs/executions/{id}/`. Capped at `EVAL_RESULT_LIST_LIMIT` (`results_truncated`). A malformed UUID is a 404, not a 500 |
| `/api/eval/runs/{run_id}/cancel/` | POST | Stop a sweep | Auth (owner) | O(1) |  | — | EvalRun | Cooperative — cases check the row on the way out of the concurrency semaphore; one already inside a model call finishes. 400 if the run already ended |
| `/api/eval/reviews/pending/` | GET | The review queue | Auth (suite reviewer or owner) | O(limit) |  | `QueueFilterSerializer` (input), `EvalResultSerializer` | EvalResult, EvalRun, EvalSuite, EvalCase | **Oldest first** — a queue is worked through. Each row carries the case `reference` so a reviewer is not deciding without the rubric |
| `/api/eval/results/{id}/review/` | POST | Record a verdict | Auth (suite reviewer or owner) | O(results in run) |  | `ReviewInputSerializer` | EvalReview, EvalResult, EvalRun | `pass`/`fail`/`unsure`. Overrides the graders for scoring but never overwrites `auto_passed` — `agreed_with_graders` is computed here and feeds `EvalRun.grader_agreement`. Re-posting is an **edit**, not a second opinion. An `error` result is a 400 (no answer to judge). Re-settles the run: `awaiting_review` → `completed` once the last verdict lands |
| `/api/eval/agents/{id}/scorecard/` | GET | Scores per suite over time | Auth (owner) | O(runs≤200) |  | — | EvalRun, EvalSuite | `latest` is null while a suite's newest sweep is still `awaiting_review` — a provisional score must not read as the current one. Each point names the `revision` it was scored under |

Tests: `eval/tests/` — `test_graders.py`, `test_supervision.py`,
`test_runner.py`, `test_api.py`, `test_public_api.py` (113 tests).

**Importing this app elsewhere:** `eval/api.py` is the public surface — pure
grading (`grade_answer`, `grade_execution`, `list_graders`, `needs_review`),
sweeps (`run_suite_now` awaited, `start_suite_run` detached), supervision
(`record_review`) and the reads. No module-level import of a sibling app, so no
cycle is possible; `eval/__init__.py` stays empty because `INSTALLED_APPS`
imports the package before the app registry is ready. See
[EVALUATION.md](EVALUATION.md) §6.

---

## Cross-cutting notes for reviewers

- **No view-level transactions.** Grep confirms zero `transaction.atomic` in any
  `views.py`. Any multi-write endpoint (workflow execute, RAG ingest, clone, deploy)
  relies on service/engine layers for consistency — **audit those, not the views.**
  (Workflow execute / clone / deploy are themselves gone; see §5.)
- **One unauthenticated write path:** the `chat/guest/*` endpoints (NVIDIA NIM
  on the pinned `GUEST_MODEL`, IP-limited). Review rate-limiting and input handling there first. The public
  webhook receiver `/api/webhooks/{user_id}/{path}` was deleted with the trigger
  runtime, so the DAG-triggering attack surface is gone entirely.
- **Known crashes still open** (from the audit): template search (KeyError 500),
  notifications create (IntegrityError 500), and a hanging endpoint (MCP `tools/`).
  (The browserOS workspace-create IntegrityError went with the app.)
  These rows are flagged / above.
- **`executor` app has views but no URL routing** — dead HTTP surface (audit §5.6);
  it's exercised only through the engine tests.
- **Thin-coverage apps** to prioritize for tests: `imagine`, `logs`,
  `templates`, `skills`. (`notifications` is now covered for the
  reminder engine; the `Notification` CRUD surface itself is still thin.)
