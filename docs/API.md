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
| `notifications` | tests (3) + tests_reminders (20) + test_scheduled_sweep (7) + test_scheduled_api (3) |  reminders + scheduled; ~ thin elsewhere |
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
| `/api/auth/register/` | POST | Register user | Public (3/min) | O(1) |  | `UserRegistrationSerializer` | User, UserProfile | **An email already registered (case-insensitive) is refused 400** (2026-09-18): sign-in and password reset look accounts up by email, and nothing stopped a second account taking one. Tests: `core/tests/test_preferences.py::RegistrationEmailTests` |
| `/api/auth/login/` | POST | JWT obtain | Public (5/min) | O(1) |  | `CustomTokenObtainPairSerializer` | User | Wrong pw → 400 (audit: should be 401) |
| `/api/auth/google/` | POST | Google login | Public | External | `core/tests/test_security_review.py` | — | User, UserProfile | Refuses `email_verified != true`. Links by email case-insensitively; linking to a never-verified password account makes that password unusable and revokes its tokens (S1, pre-account takeover). Sets `email_verified_at` |
| `/api/auth/token/refresh/` | POST | Refresh JWT | Public | O(1) | `core/tests/test_security_review.py` | `RevocableTokenRefreshSerializer` | token_blacklist_* | Refuses a token older than `tokens_valid_after` (S2). Rotation blacklists the used token — really, since `token_blacklist` was installed 2026-09-25 (S11) |
| `/api/auth/profile/` | GET/PUT/PATCH | Read/update profile | Auth | O(1) |  | `UserProfileSerializer` | User, UserProfile | Writes `vision_provider`/`vision_model` — the witness `ask_vision` calls (docs/VISION_AGENT.md). **`credits_remaining` and `llm_credential_id` are read-only** (2026-09-18): the balance was writable, so any user could PATCH an unlimited platform-key allowance. `timezone` must be an IANA zone (400 otherwise) and `language` is stored as a code whichever spelling is sent (`English` → `en`), because both now reach the model: `core/preferences.py` renders timezone, language, `display_name` and `bio` into chat's and every agent run's system prompt, and the timezone drives chat's clock and an agent's `useEnvironment` time. A nested `user.email` already held by another account (case-insensitive) is refused 400 — sign-in and reset look accounts up by email; changing it still has no verification step. Tests: `core/tests/test_preferences.py` **A nested `user.email` that differs from the current one is refused 400** — change it through `auth/email/change/`; the unchanged address the form always sends is fine. `default_max_tokens` is read-only (stored, never read, and a small cap empties a reasoning model's answer) |
| `/api/auth/profile/avatar/` | POST | Upload avatar | Auth | O(1) | ~ | — | UserProfile | Multipart |
| `/api/auth/change-password/request-otp/` | POST | OTP to change pw | Auth-intended | O(1)+email | ~ | — | PasswordOTP | Returns 401 on empty body (audit) |
| `/api/auth/change-password/verify-otp/` | POST | Verify change OTP | Auth-intended | O(1) | ~ | — | PasswordOTP | |
| `/api/auth/email/change/request/` | POST | Send a code to a new email address | Auth (throttled, `password_change` scope) | O(1) |  | — (`new_email`, `password`) | PasswordOTP | The **only** way to change the sign-in address (2026-09-18). The code goes to the **new** address, which proves it is the user's; the current password is required where the account has one (a Google-only account has none). 400 for an invalid address, the current address, a wrong password, or an address another account holds. Reuses `PasswordOTP` with purpose `email_change` and `target_email`. Tests: `core/tests/test_preferences.py::EmailChangeTests` |
| `/api/auth/email/change/confirm/` | POST | Apply the new email once its code comes back | Auth (throttled) | O(1) |  | `PasswordOTPVerifySerializer` | PasswordOTP, User | Same attempt limit and 10-minute expiry as the password codes. Re-checks the address is still free — it may have been taken while the code was valid |
| `/api/auth/change-password/` | POST | Change password | Auth | O(1) | `core/tests/test_security_review.py` | — | User, PasswordOTP, UserProfile | Revokes every token (S2) and returns a fresh `access`/`refresh` for this tab; marks the email verified |
| `/api/auth/password-reset-request/` | POST | Reset OTP email | Public (10/day) | O(1)+email |  | — | User, PasswordOTP | |
| `/api/auth/password-reset-verify/` | POST | Verify reset OTP | Public (10/day) | O(1) |  | — | PasswordOTP | |
| `/api/auth/password-reset-confirm/` | POST | Set new password | Public (10/day) | O(1) | `core/tests/test_security_review.py` | — | User, PasswordOTP, UserProfile | Revokes every token (S2); marks the email verified |
| `/api/auth/api-keys/` | GET/POST | List/create API keys | Auth | O(1) | `core/tests/test_security_review.py` | `APIKeySerializer` | APIKey | Only SHA-256 stored (S6); plaintext is `api_key` in the create response and nowhere else — the list returns `key_prefix`, never `key` |
| `/api/auth/api-keys/{pk}/` | GET/PUT/PATCH/DELETE | API key CRUD | Auth (owner) | O(1) | ~ | `APIKeySerializer` | APIKey | |
| `/api/auth/api-keys/{pk}/rotate/` | POST | Rotate a key | Auth (owner) | O(1) | `core/tests/test_security_review.py` | — | APIKey | `new_key` is the only copy of the plaintext |
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
| `/api/llm/models/` | GET | List AI providers + models | Auth | O(n) |  | — (hand-built payload) | AIModel, AIProvider, Credential | Filtered to `llm.providers.SUPPORTED_PROVIDERS`, so retired rows left in the table by an un-reseeded instance are not offered. Availability per provider uses `credentials.resolution` (`slugs_for` / `platform_api_key` / `KEYLESS_PROVIDERS`) — the same lookup the executor runs, so what the picker shows is what will actually execute. Each model also carries `effort_levels` / `default_effort` / `supports_effort` (`llm/effort.py`): which reasoning-effort rungs it serves, cleaned to the ladder before it is sent, with `[]` meaning *no effort control* rather than *unknown*. The picker renders the control off that array, so a level the runtime would refuse is never offered. Top-level `meta` carries `last_refresh` (last completed live refresh, null when none has ever run) and `fallback` (the platform fallback pair) — alongside, never inside, `providers`, so old bundles keep reading. |
| `/api/llm/models/refresh/` | POST | Live catalogue refresh: diff OpenRouter `/v1/models` against held rows | Staff | External (lock-serialised) | `llm/tests/test_refresh.py`, `test_models_endpoint.py` | — | AIModel, AIProvider, SubAgent, ModelFallbackNotice, Notification | Same service host cron drives (`manage.py refresh_models`); see `llm/catalog_refresh.py`. Seen-live rows get pricing/context/`last_seen_at` (caps and effort stay hand-curated); new ids are created `is_active=False, source='live'` for staff to activate; held `curated`/`live` rows missing upstream retire with `retired_at` (never deleted; `hand` rows untouched; `RETIRED_MODEL_VALUES` always wins). Retired ids fan out to one `system` notification per affected owner (`data.kind='model_retired'`), rate-limited by `ModelFallbackNotice`. 409 `refresh_in_progress` on lock contention, 400 `refresh_refused` with no key or an empty upstream (nothing is written then). |
| `/api/llm/fallback/` | GET, PATCH | Read (all) / change (staff) the platform fallback model | Auth / staff | Light | `llm/tests/test_models_endpoint.py`, `test_fallback.py` | — | ModelFallback, AIModel | The pair runs execute on when their configured model is retired or unknown (`llm/fallback.py::resolve_with_fallback`, read with a 60s TTL so edits apply without restart). PATCH refuses typos against a held catalogue but allows them on an empty one (fresh install), mirroring `AgentSerializer._model_problem`; a retired id saves with a warning. |

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
[system.py](../agents/views/system.py),
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
| `/api/orchestrator/hitl/pending/` | GET | Pending human-in-the-loop requests | Auth | O(n) |  | `HITLRequestSerializer` | HITLRequest | Security-tested. Joins `execution__subagent`; it selected `execution__workflow` until 2026-08-24 and 500'd on every call — `agents/tests/test_regressions.py::RenamedColumnTests`. **Returned an empty list on every call until 2026-09-01**: nothing outside the test suite had created a `HITLRequest` since the DAG supervisor was retired, so this endpoint, the escalation ladder and the daily digest were all unreachable. `agents/agent/hitl.py::open_request` is the missing write, called from `stream._approval_requested` when an agent run pauses; `resolve_request` closes the row on approve/reject. Tests: `notifications/tests/test_agent_notifications.py`. Rows carry a `detail` field as of 2026-09-05 (`chat/tools/describe.py::describe_call`) — the call as a person reads it, lifted out of `context_data` by the serializer so the thread and agent ids stay off the wire. `title` used to be `f'Approve {tool}?'`, which for the calls that matter most read `Approve mcp__7__send_email_ab12cd34?` and named no arguments at all, because `_approval_requested` dropped them. Null on every row written before that, so the Inbox falls back to `message`. **`question`** (2026-09-25): an `ask_user` question's shape (kind, options, min/max/step, unit) on `clarification` rows, lifted out of `context_data` the same way, so the Inbox draws the chat's question card |
| `/api/orchestrator/hitl/{request_id}/respond/` | POST | Answer a HITL request **and resume the run** | Auth (owner) | Heavy | ~ | — | HITLRequest, ExecutionLog, ToolPermission | **Resumed nothing until 2026-09-05.** It set `status`, wrote `responded_at` and returned 200 — its own comment deferred the resume to `agents/{id}/approve/`, which the Inbox has never called. So answering here recorded the decision, took the row out of the queue, stood the reminder ladder down, and left the agent parked on its `interrupt()` with nobody left to notice: a dead end that looked like a working screen. It now runs the same three steps as `agent_approve`, through the same functions — `approve_tool_call` / `reject_tool_call`, close the row, `resume_agent_run` — resolving `(thread_id, call_id)` out of `context_data`. `scope` is accepted here too (`once`\|`session`\|`always`). Three things are deliberate: the `status='pending'` filter is the only guard against the live socket and the Inbox both answering (second answer 404s, one resume); closing the row is **unconditional** while the resume is not, so a `clarification` row or one written before `context_data` carried a thread id still closes rather than 500ing; and a resume that raises is logged, not surfaced — the answer is already recorded and the user has no second copy of it. **Clarification rows resume too** (2026-09-25): `action: 'respond'` with `response`/`value` (option text, list, number or sentence) goes through `chat.turn.agent.answer_question`, checked against the question the run asked (400 when it does not fit), then the run resumes with it as `ask_user`'s result; `action: 'skip'` rejects the question so the run proceeds on its stated assumption; a row with no paused run behind it closes as answered. Tests: `agents/tests/test_hitl_inbox.py`, `chat/tests/test_questions.py` |
| `/api/orchestrator/agents/` | GET/POST | List / create agent (a `Workflow` with `kind='agent'`) | Auth | O(n) |  | `AgentSerializer` | Workflow, ExecutionLog, Trigger | `agents/tests/test_agents.py`, `agents/tests/test_run_limits.py`; stats counted from the log, not stored — `runs`/`unattended` are **distinct** counts (the `hitl_requests` filter forces a LEFT JOIN that multiplied both, and the spend, by the approval count) and `spend` is rupees derived from `tokens_used` via `agents/spend.py`, the same number the spend cap refuses on. A non-blank `schedule` reconciles a `Trigger` row via `AgentSerializer.sync_schedule` **after** save, and is refused unless `allowUnattended` is also on — the runtime rejects every unattended firing otherwise. `cpu` and `memoryMb` were **removed from the wire** (2026-08-29): they were stored, validated and read by nothing, and could not be enforced by a sandbox that `exec`s on a thread in this process. `maxRunSeconds` replaces them and *is* enforced — `agents/budget.py`, clamped to `MIN_RUN_SECONDS`..`MAX_RUN_SECONDS`. The three context-lifecycle booleans (`compaction`, `recursiveContext`, `indexing`) are **read at run time** as of 2026-09-01 — `chat/turn/curation.py` — and `summaryModel`/`summaryProvider` were added beside them: which model folds a long run's earlier steps, blank meaning the platform default (`CONTEXT_SUMMARY_MODEL`, a small NVIDIA model the platform holds a key for). `agent_context['knowledgeBases']` is likewise enforced now rather than merely printed into the prompt: it becomes `TurnContext.kb_scope`, and every KB tool filters on it — an empty selection still means *unrestricted*, since agents predating enforcement never had one applied. **`connectors` is enforced as of 2026-09-01 and now holds `MCPServer` ids**, validated against the connections the caller can actually see (`mcp_integration.client.visible_server_ids_sync`, curated rows included). It was a hardcoded set of six presentation slugs that nothing on the run path read, so the `mcp` grant resolved *every* connection the account owned; it is now the second axis to that grant, the way `fileAccess` is to `fileOps`. Empty means *unrestricted* for the same reason `knowledgeBases` does. Enforced at both doors — `AgentToolbox.descriptors` narrows what is offered and `AgentToolbox.mcp_server_allowed` re-checks at dispatch, since a model names tools it saw in an earlier turn. Legacy slug values are cleared by `orchestrator.0021` and skipped at read time. Tests: `agents/tests/test_connector_scope.py`. **`effort`** (2026-09-03) is the model's reasoning-effort level, stored in `runtime_settings` beside `temperature` and read by `agents/agent/runtime.py` into `TurnContext.effort`. Validated against `llm.effort.LADDER` rather than against the chosen model's own rungs — the model can change in the same PATCH, and `llm.access` snaps a level the model does not serve. Blank means the model's own default, which is what every agent saved before the field existed keeps: the same rule `connectors` and `knowledgeBases` follow, for the same reason. Tests: `agents/tests/test_effort.py` **Config surface changed 2026-09-03.** Added, all of them fields the runtime already read and nothing could set: `description` (what `search_agents` shows a delegating agent — blank on every agent ever built here until now), `tags`, `status` (writable: draft|active|paused, `archived` is not offered because there is no un-archive path), `outputContract` (the closed registry in `agents/contracts.py` — `research`|`extraction`|blank for prose, resolved by `contracts.resolve` on every run) and `fanoutParallel` (`run_fanout`'s width; `fanout['mode']` stays unexposed because nothing reads it). Also `delegatesTo` — `SubAgent` ids, the second axis to the `subAgents` grant, empty meaning any the user owns. **Removed:** `workdir`, `venv` and `useOrgContext` (stored, validated, read by nothing — the `cpu`/`memoryMb` story again); `egress` (read only to add a prompt sentence, and its two wider values were unimplementable on a sidecar with no network — the sentence is now unconditional); and `trigger`, which is now *derived* from whether a schedule exists and emitted read-only, since that was the only rule it ever carried. **`connectors` accepts two shapes**: a bare id (every pre-existing agent, meaning every tool that connection offers) or `{id, mode, tools}` with mode `all|read|selected` — see the connector-scope note below. Tests: `agents/tests/test_agents.py`, `agents/tests/test_delegation_scope.py`, `agents/tests/test_connector_scope.py` Each agent now carries **`model_status`** (`ok` | `retired`), computed in `_with_stats` rather than `to_config` so a model retired upstream never mints a revision. **`provider`/`model` and `summaryProvider`/`summaryModel` are validated against `AIModel`** when that provider has catalogue rows (400 naming the other provider when the id belongs elsewhere); a provider with no rows is not policed, so a fresh install still works. `status` accepts **`archived`** (restored from the agents list). `reviewAgent` is gone from the wire (read by nothing). Tests: `agents/tests/test_agent_lifecycle.py` **`tools.office`** (2026-09-19) is a new grant key in `TOOL_KEYS`: it unlocks `render_deck` / `render_workbook` / `render_document` (`chat/tools/office/`). Like `fileOps` it needs a `fileAccess` other than `none` — `build_file_scope` now builds a scope for either grant, and with no scope the toolbox withholds both sets. Tests: `chat/tests/test_office.py::OfficeAvailabilityTests`. **2026-09-20: `tools.media`, `tools.publish`, `tools.browser` and `browserDomains`.** `media` unlocks `generate_image` (needs `fileAccess`, like `office`); `publish` unlocks `publish_page`; `browser` unlocks `browse_page` + `browser_act`, both withheld unless `BROWSER_ENGINE=remote` is configured. `browserDomains` is a list of hostnames `browser_act` may act on (subdomains included, max 20, normalised); **empty means read-only** — unlike the older scopes, because it arrived with the tool. It is in `publishing.SHAREABLE_KEYS` (a domain list names nothing private). Tests: `chat/tests/test_browser.py`, `chat/tests/test_media.py`. **2026-09-20: `toolScope`** — the third axis after the grant (may it at all) and the scope (which rows): *which tools*, a list of built-in tool names, **empty meaning every tool its grants unlock**. Validated against `GRANT_TOOLS` (a name no grant can unlock is a 400, not a silently dead entry), never narrows `ALWAYS_AVAILABLE`/`RETRIEVAL_TOOLS`, and cannot widen past the grants. MCP tools are out of scope by design — their names are minted at runtime, so `connectors` answers that question per connection. **2026-09-21: `toolPermissions`** — the fourth axis: *how* each built-in tool may be used, `{name: allow|ask|deny}`, empty meaning grants + `toolScope` + autonomy decide alone. Same closed-world validation as `toolScope` (unknown names and MCP/infra names are 400); `deny` withholds and refuses at dispatch, `ask` forces approval even under `auto`/`full`, `allow` skips the autonomy pause without widening past the grants; workers inherit most-restrictive-wins so delegation cannot widen a parent rule. Tests: `agents/tests/test_tool_permissions.py`. Read-only `template_slug` names the catalogue entry the agent was installed from (`null` for handbuilt agents) — attached in `_with_stats`, never in `to_config`, so it does not pollute the revision snapshot; what the Explore page joins on to show "installed" and offer uninstall (which is the agent's own DELETE). Tests: `agents/tests/test_gallery.py::ExploreGroupingTests`. Tests: `chat/tests/test_new_tools.py::ToolScopeTests` |
| `/api/orchestrator/agents/{id}/` | GET/PUT/PATCH/DELETE | Agent detail | Auth (owner) | O(1) |  | `AgentSerializer` | Workflow, Trigger | PATCH **merges** onto the stored config — a partial save must not reset an unsent grant, `allowUnattended` included. PUT omits nothing: an unsent `allowUnattended` reads as False. `sync_schedule` runs **before** `revisions.record`, so the revision snapshots the schedule as saved rather than the one it replaced **DELETE keeps the agent's runs and revisions** (2026-09-18): `ExecutionLog.subagent` and `SubAgentRevision.subagent` are `SET_NULL` (migration `logs.0019`), so run history and recorded spend survive; a deleted agent's runs are named from their revision snapshot as "<name> (deleted)". Archive (`status: archived`) is the restorable alternative the builder offers first |
| `/api/orchestrator/agents/{id}/revisions/{number}/restore/` | POST | Put the configuration back to an earlier revision | Auth (owner) | O(1) |  | `AgentSerializer` (the revision snapshot, re-validated) | SubAgent, Trigger, SubAgentRevision | An ordinary save of the snapshot through the same serializer, ownership checks and `sync_schedule` — so a connection or skill the old config named that has since gone is the serializer's own 400, never a silent re-grant. Recorded as a **new** revision with source `restore` (history is never rewritten). `status` and observed stats are not restored. Tests: `agents/tests/test_agent_lifecycle.py::RestoreRevisionTests` |
| `/api/orchestrator/agents/{id}/builder-chat/` | GET | The builder conversation kept for a saved agent, oldest first | Auth (owner) | O(40) |  | — | ConversationMessage | Written by `agents/configure/` when it is given `agent_id`; newest 40 kept (`BUILDER_CHAT_KEEP`), stored as `ConversationMessage(metadata.surface="builder")` with each reply's proposed `changes`. 404 for someone else's agent. Tests: `agents/tests/test_builder.py::BuilderChatMemoryTests` |
| `/api/orchestrator/runs/{execution_id}/cancel/` | POST | Stop a run | Auth (run owner) | O(1) |  | — | ExecutionLog, HITLRequest | **By execution, not agent** (an agent may have several runs). A task in this process is cancelled and its own `CancelledError` branch closes it (`_finalise_cancelled`); a run **paused for approval** has no task, so it is closed here with its pending approval withdrawn and checkpoint dropped. **409** for a delegated worker (it runs inside its parent — stop that) and for a `running` row with no task in this process (another worker, or an orphan `recover_runs` will close), rather than writing `cancelled` over a run still going elsewhere. A **detached coding-task worker** is the exception to the 409: it runs in its own spawned task while the lead waits in `wait_tasks`, so stopping it alone is safe — `chat/tools/tasks.py::_cancel_detached` mirrors the two in-process branches without the delegation guard, and closes the lead's task record too. 404 for a malformed id. Closed runs now keep their turns' token total instead of recording 0. Tests: `agents/tests/test_run_stop.py`, `agents/tests/test_code_dispatch.py` |
| `/api/orchestrator/runs/{execution_id}/steer/` | POST | Steer one coding-task worker mid-run | Auth (run owner) | Light |  | `RunSteerSerializer` (`message`) | ExecutionLog | The plan panel's Steer button. `agents/{id}/steer/` addresses the latest running run of an *agent* — the wrong worker when one implementer has two going — so this addresses the *execution* the lane already shows and looks up the thread the same way the run does (`input_data.thread_id`). Same mailbox, caps and stats as a user steer; 404 for a foreign or malformed id, 409 when the worker is not running |
| `/api/orchestrator/runs/{execution_id}/autonomy/` | POST | Change one coding-task worker's autonomy mid-run | Auth (run owner) | Light |  | `AgentAutonomySerializer` (`level`) | ExecutionLog | The panel's per-worker Ask… switch (`review` \| `ask` \| `auto` \| `full`, never `plan`). Not retroactive, like `agents/{id}/autonomy/`: a call already paused still needs an answer |
| `/api/orchestrator/agents/configure/` | POST | The builder's chat: a plain-language description in, agent knob changes out | Auth | Heavy (one model call) |  | `ConfigureSerializer` (`message`, `config`, `history`) | MCPServer, KnowledgeBase, Skill (read-only) | **Proposes; saves nothing** — the board applies the changes and the user still PATCHes `agents/{id}/`. Replaces the browser-side keyword table (`src/lib/agentProposals.ts`), which moved a knob only when the description happened to contain one of its words. Nothing the model returns is trusted: `builder.KNOBS` is one table used twice — to describe each knob in the prompt and to validate the value that comes back — so an unknown path, an out-of-range value or an id the caller cannot see is *dropped*, never corrected. Connector / KB / skill ids come from the caller's own catalogue (the same `_visible_servers_queryset` predicate the runtime resolves the toolbox through), and the merged config is re-validated through `AgentSerializer` so a proposal can never be one the user is then unable to save. Sits above `agents/<int:agent_id>/` only by convention — the int converter would not match `configure`. **503 with `code: builder_model_unavailable`** when no provider answers, which is what tells the browser to fall back to its local rules; the agent's own provider/model is tried first, then `AGENT_BUILDER_*` / `CONTEXT_SUMMARY_*` (a platform key, so the builder works for a user who has connected nothing). `provider`, `model` and the summary pair are deliberately not settable — a model id is a routing key, and a guessed one is a run that dies at its first call. Tests: `agents/tests/test_builder.py` The catalogue now also carries the user's own agents (for `delegatesTo`), and the connector knob accepts `{id, mode: 'read'}` — `selected` is deliberately not offered to the model, since naming individual tools needs a live catalogue this endpoint does not fetch and a model doing it would be guessing names The prompt now carries **the user's Settings timezone** so a cadence is scheduled in their zone, and **offers neither `tools.shell` (in `runtime.UNSERVED_GRANTS`) nor `reviewAgent` (read by nothing)**, and no longer asks for the retired `trigger` field Optional **`agent_id`**: the exchange (message, reply, changes) is kept for that agent and read back by `agents/{id}/builder-chat/` |
| `/api/orchestrator/agents/{id}/execute/` | POST | Start an agent run against a goal | Auth (owner) | Heavy |  | `AgentExecuteSerializer` | Workflow, ExecutionLog | **202 + `execution_id`**, run is detached via `background.spawn()`. Guardrails **and the provider credential** are checked *before* responding, so 402 reaches the caller rather than killing a run that looked started — no credential for the agent's `llm_provider` → 402 naming the provider; a provider with no handler → 400. Steps stream to `ws/execution/{id}/` — [agents/agent/stream.py](../agents/agent/stream.py). A `thread_id` naming a paused run **resumes** it on its original `execution_id` rather than opening a second log against the same checkpointer key; an unknown one falls through to a normal start. The spawned run then takes an **admission slot** (`agents/admission.py`, per-user and global, per process) before doing any work, so the 202 is "accepted", not "started" — a queued run sits at `running` with no steps, and one that never gets a slot within `ADMISSION_WAIT_SECONDS` closes as `failed` saying so. A run that outlives `guardrails['maxRunSeconds']` closes as **`timeout`**, a status distinct from `cancelled`: the first is a limit the owner can raise, the second is a person having pressed stop Called from the web app as of 2026-09-18 (**Run** on the builder and on each agent card, `RunAgentDialog`), which then opens `/runs?run=<execution_id>`. **A `paused` agent** is refused (`AgentPaused`) for unattended callers only — schedules, webhooks and delegation — never for its owner here; the schedule sweep skips its slot as outcome `paused` rather than counting a failure |
| `/api/orchestrator/agents/{id}/approve/` | POST | Approve a paused tool call **and resume the run** | Auth (owner) | Heavy |  | `AgentApproveSerializer` (`thread_id`, `call_id`, `scope`, `remember`) | ExecutionLog, AgentTurn, AgentStep, ToolPermission | Ownership re-checked: a thread id is not an authorisation. Resumes on the *original* `execution_id` so the trace stays one run. `scope` is `once` \| `session` \| `always`; `session` files the allowance against `ToolPermission.session_key` so it expires with the run, which is the rung users actually want and had to grant `always` to get. `remember: true` is the retired spelling of `always` and still works — blank `scope` is what lets the two be told apart. Also closes the run's `HITLRequest` (`agents/agent/hitl.py::resolve_request`) **before** resuming, which is what cancels the escalation ladder — `resume_agent_run` reopens the log, so closing afterwards would race it and leave an answered question nudging |
| `/api/memory/` | GET/DELETE | Read or clear every durable fact the assistant has stored about you | Auth | O(n) |  | — | core.UserMemory | Read-and-delete only, no create: a fact typed into a settings screen is a preference and belongs on the profile. This surface exists to audit and correct what the assistant *inferred*, because every row rides in the system prompt of every future turn |
| `/api/memory/{id}/` | DELETE | Forget one fact | Auth (owner) | O(1) |  | — | core.UserMemory | Scoped by `user=` in the query, so another user's id and a nonexistent id are both 404 — "exists but not yours" is an ownership oracle |
| `/api/orchestrator/agents/{id}/steer/` | POST | Send a mid-run instruction to a run already going | Auth (owner) | Light |  | `AgentSteerSerializer` (`message`) | SubAgent, ExecutionLog | Lands in the in-process steer mailbox keyed by the run's `thread_id`; the graph's `steering` node picks it up at its next tool boundary. Same run, same log, same stream — no restart. 404 when nothing is running. **Queued, not last-write-wins (2026-09-04)**: several messages sent while the agent works are all delivered, in order, as one user message. Response carries `delivered` / `queued` / `dropped` (`replaced` is gone) |
| `/api/orchestrator/agents/{id}/autonomy/` | POST | Change how much a running agent asks, mid-run | Auth (owner) | Light |  | `AgentAutonomySerializer` (`level`) | SubAgent, ExecutionLog | The counterpart to `steer/`, sharing its mailbox and its run lookup. `level` is `review` \| `ask` \| `auto` \| `full` — **not `plan`**, because which tools exist is settled when the toolbox is built, so a mid-run `plan` could only gate the mutating tools rather than withdraw them. Takes effect at the next tool batch and is *not* retroactive: a call already paused still needs an answer, since the looser setting arrived after the question. 404 when nothing is running, 409 when the run has no thread id |
| `/api/orchestrator/templates/` | GET | Everything installable: curated templates **and** agents other users published | Auth | Light | `agents/tests/test_gallery.py::GalleryReadTests`, `agents/tests/test_sharing.py::ExploreTests` | — (plain dicts) | SharedAgent, MCPServer, KnowledgeBase, Skill (read-only) | **Two sources, one shape.** A *curated* entry is code — `agents/gallery.py`, per the note in `templates/models.py`: a template is a `SubAgent` used as a starting point and needs no table. A *community* entry is a `SharedAgent` row. They are presented with the same keys and installed by the same function, because provenance changes how much an installer trusts an entry, not what it is; `source` says which. Each carries a flat `AgentConfig` under `config`, which is what the install screen renders its permissions from, so the screen and the runtime read the same keys. `requirements` name *kinds* (`connector` / `knowledge_base` / `skill`), never ids — an entry pointing at knowledge base 2 would, installed elsewhere, silently read somebody else's row 2. `candidates` are computed per requirement from the caller's own rows via the **same predicate the serializer validates against** (`visible_servers_sync`); a `provider` hint reorders that pool and never filters it. Listing shows `visibility='platform'` shares only — a `link` share is reachable by slug and nowhere else, which is the entire difference between the two — plus the caller's own rows whatever their visibility, so publishing something unlisted does not look like it failed. `?source=curated\|community` narrows; `?mine=1` returns the caller's own publications including withdrawn ones (which is why the frontend filter is a server parameter and not a client-side `.filter()`). Each curated entry names its one-click pack under `pack` (computed from `gallery.PACKS`, never stored, so the catalogue cannot disagree with the pack; `null` for entries in no pack and for every shared entry) — what the Explore page groups by. Every entry also carries `available` + `unavailable_reason` (2026-09-24): whether it can run on this server at all, from the one predicate `agents/views/capabilities.py::unavailable_grants` (a grant whose engine is `none` - workspace, browser, e-sign, speech). Explore badges an unavailable entry and disables its install; the anonymous public projection carries `available` only |
| `/api/orchestrator/templates/{slug}/` | GET | One entry, same shape as the listing | Auth | Light | `agents/tests/test_gallery.py::GalleryReadTests`, `agents/tests/test_sharing.py::ExploreTests` | — | SharedAgent | Curated slugs are looked up first, which is why `_mint_slug` checks the gallery as well as the table — the two share a namespace and a shared agent that took a curated slug would be permanently unreachable. A `link` share resolves here for anybody holding the slug: that *is* the sharing mechanism. A withdrawn share resolves only for its author, so they can relist it; 404 for everyone else |
| `/api/orchestrator/templates/{slug}/install/` | POST | Create one of the caller's own agents from a curated template or a shared agent | Auth | Light (no model call) | `agents/tests/test_gallery.py::InstallTests`, `agents/tests/test_sharing.py::ExploreTests` | `AgentSerializer` | SubAgent, SharedAgent, Trigger, SubAgentRevision | Body `{name?, requirements: {key: id}, timezone?}`. **Writes through `AgentSerializer`, not around it** — install is the same act as saving in the builder, so it gets the same closed tool-grant set and the same ownership re-check on every id; a second write path is a second place to forget a guardrail, and here that check is the whole reason installing a stranger's agent is safe (`test_a_shared_agents_requirements_are_resolved_against_the_installer` supplies the *author's* KB id and gets a 400). A required requirement left blank is a 400 naming it. `timezone` applies only when the entry ships a cron — the author's own zone is deliberately not carried, since "Monday morning" is a different instant for whoever installs it — and the resulting `Trigger` is `origin='builder'`. Duplicate names are suffixed like `agents/`; the agent is tagged `template:<slug>` or `shared:<slug>`, and `revisions.record(source='create')` writes revision 1. A community install bumps `install_count` with `F()`, not read-modify-write. 409 (2026-09-24) when the entry holds a grant whose engine is `none` on this server - installing it would write an agent that can only talk |
| `/api/orchestrator/templates/install-pack/` | POST | Install a pack (e.g. `{"pack": "office"}`) in one click | Auth | Light (no model call) | `agents/tests/test_install_pack.py` | `AgentSerializer` | SubAgent, SubAgentRevision | Installs every template in `gallery.PACKS[pack]` whose requirements are all optional and which the caller has not already installed (checked via `SubAgent.template_slug`, set by both single and pack installs). Required-requirement templates are skipped as `{slug, reason: 'needs setup'}` rather than installed half-configured; already-installed as `'already installed'`. Idempotent: reinstalling installs nothing. Optional **`overrides: {slug: AgentConfig-fragment}`** — per-template tightening (`autonomy`, `writePaths`, `commandScope`, `toolPermissions`, `toolScope`, `spendCapRupees`, `playbooks`) through the same serializer the builder saves through, so the install screen's matrix and the runtime read one copy; a bad override skips only that template as `'invalid configuration'`. Installing the **`code` pack wires the lead**: `coding-lead`'s `delegatesTo` is set to the installed roster ids (recomputed over earlier installs too), so a lead cannot delegate outside the roster. Members holding a grant whose engine is `none` are skipped as `engine unavailable: <reason>` rather than installed unable to run; when *every* setup-free member is engine-blocked the pack answers 409 naming the engine instead of an empty install (the Code pack on a default install). The management command `install_packs` skips the same members as `engine unavailable`. Returns `{pack, installed: [{slug, id, name}], skipped: [{slug, reason}]}` |
| `/api/orchestrator/agents/{id}/share/` | GET/POST/DELETE | Preview, publish, or withdraw one of the caller's own agents | Auth (owner) | Light | `agents/tests/test_sharing.py` | — (hand-rolled; `publishing.to_shareable` is the projection) | SharedAgent, SubAgent | **GET writes nothing** and returns the whole payload publishing *would* send, which is the point: the author sees the allow-listed config and every requirement their row ids became before confirming. The projection (`agents/publishing.py`) is an **allow-list** (`SHAREABLE_KEYS`), never `to_config()` minus a few keys — a denylist publishes every field added to `AgentConfig` later, and the first one carrying something private is a leak nobody wrote a line of code to cause. Every id becomes a requirement or the publish **fails**; dropping it is how an agent arrives in a stranger's account missing the corpus it was written around. Requirement labels default to the source rows' own names (a fact about the author's account), which is why they are shown editable — `sanitise_requirements` takes the author's wording and refuses their `key`/`type`. `visibility` is a three-rung ladder — `link` (by slug, still needs an account) < `platform` (listed to signed-in users) < `public` (also readable with no account, through the unauthenticated pair below) — and it **defaults to `platform`**, never the widest rung. POST is create-or-republish: the slug is **kept** across a rename so an existing link cannot rot, and `version` is bumped. DELETE **unlists rather than deletes** — copies already installed keep working and relisting reuses the URL. The listing is a **snapshot**: editing the agent afterwards does not change what strangers already approved, and `subagent` is `SET_NULL` so deleting your own agent does not retract what others are installing |
| `/api/orchestrator/capabilities/` | GET | What an agent may be granted: tools, scope field and engine state per grant | Auth | Light | `agents/tests/test_capabilities.py` | — (plain dicts) | — | Derived from `GRANT_TOOLS` + `UNSERVED_GRANTS` + engine availability rather than hand-written, so the builder can grey out what cannot run. Per grant: `tools` (exactly the names the grant unlocks), `scope` (the `agent_context` field for *which* rows/hosts, or null), `engine_live` + `engine_reason` (a grant whose engine is `none` says why — e.g. browser with no browser configured), `risk` (one line), `served`. Tests pin the tools match the runtime grant for grant |
| `/api/orchestrator/public/agents/` | GET | Publicly shared agents | **AllowAny** | Light | `agents/tests/test_sharing.py::PublicCatalogueTests` | — (`_present_public`) | SharedAgent | The second unauthenticated route in this app, after the webhook receiver, and it follows that route's rules for the same reason. `visibility='public'` and `is_listed` only. **A narrower projection than the signed-in one** — `_present_public` is its own function, not `_present_shared` with a flag: the signed-in shape carries `is_mine` and per-requirement `candidates`, both computed from a caller that does not exist here, and a flag on one function is how account-shaped fields eventually leak into an anonymous response. Capped at `PUBLIC_CATALOGUE_LIMIT` (60) with `truncated` in the body — DRF pagination never applies to `@api_view` function views, and an uncapped list reachable with no account is a free full-table scan. Throttling *is* inherited: `DEFAULT_THROTTLE_CLASSES` applies to function views, so `AnonRateThrottle` (100/hour) covers it |
| `/api/orchestrator/public/agents/{slug}/` | GET | One publicly shared agent | **AllowAny** | Light | `agents/tests/test_sharing.py::PublicCatalogueTests` | — | SharedAgent | **Every refusal is the same 404, body included.** A `link` share, a `platform` share, a withdrawn one and a slug that never existed must be indistinguishable from outside, or this becomes an oracle for enumerating what people published privately — the same rule `hooks/{secret}/` states. Carries no row ids (they left as requirements at publish time) and credits the author by display name, never email. Installing still requires an account: there is no public install route |
| `/api/orchestrator/triggers/` | GET/POST | List / create triggers (schedule, webhook, event) | Auth (owner) | Light | `agents/tests/test_schedules.py::TriggerRepresentationTests` | `TriggerSerializer` | Trigger, SubAgent | Cron validated by `agents/triggers.py` **in the row's own `timezone`**; creating a schedule arms `next_due_at` (UTC) from `starts_at` where set. `subagent` ownership checked in `validate_subagent`. Refuses a syntactically valid expression with no next run (`0 0 30 2 *`) and a backwards `starts_at`/`ends_at` window. Each row carries `description` (the cron in words) and `upcoming` (next 3 firings) so a listing shows meaning, not syntax. `origin` is read-only and forced to `manual` on create — only `AgentSerializer.sync_schedule` may claim the `builder` row. `?agent=<id>` filters; capped at `TRIGGER_LIST_LIMIT` (200) and says `truncated` in the body when cut. An **enabled** schedule or webhook on an agent without `allow_unattended` is refused (`subagent`: enable it first) - the runtime would refuse every firing and five refusals self-disable the row, so this is the builder's schedule/`allowUnattended` pair rule on the manual path. A disabled trigger may still be saved. `mode='event'` is refused outright - event triggers have no runtime yet.
**webhook** needs no cron but is refused when it has neither a `goal` nor an agent `prompt` (`agents/tests/test_triggers.py::WebhookCreationTests`) — the receiver's own 404 for that case is indistinguishable from a wrong secret by design, so the hook would be silently dead for ever. `agent_has_prompt` is returned so an editor knows which of the two is missing |
| `/api/orchestrator/triggers/preview/` | POST | Dry-run a cron expression: what it says in words, and its next firings | Auth | Light | `agents/tests/test_schedules.py::SchedulePreviewTests` | `SchedulePreviewSerializer` (input only) | — | Saves nothing. Answers **200 with `valid: false`** for a bad expression rather than 400 — the caller is a field the user is still typing in, and a 400 per keystroke is an error report, not feedback. Body: `{cron, timezone, count<=10, starts_at, ends_at}` → `{valid, error, description, upcoming[]}`. Deliberately the *server's* cron reader, not a mirror of the client's: the only reading that matters is the one `agents/sweep.py` will act on |
| `/api/orchestrator/triggers/health/` | GET | Scheduler liveness: is anything running the sweep, and when did it last tick | Auth | Light | `agents/tests/test_scheduler.py::SchedulerLoopTests` | — | SchedulerLease | `{running, last_tick_at}`; `running` means `beat_at` within two lease periods. The Schedules page polls it for the red banner — a stopped scheduler must say so instead of showing cards that look healthy while nothing fires. `last_tick_at` is null when no process has ever held the lease |
| `/api/orchestrator/triggers/{id}/` | GET/PATCH/DELETE | One trigger | Auth (owner) | Light |  | `TriggerSerializer` | Trigger | Re-enabling clears `consecutive_failures`, else it fires once and self-disables 
again. `webhook_url` exposes the secret to the owner only. PATCHing re-arms through 
`_arm` only when the schedule itself moved (cron/timezone/window) or the row was re-enabled — a goal rename no longer recomputes `next_due_at` or clears an owed `queued_for`, which used to silently skip a due firing; `origin` cannot be set over the wire |
| `/api/orchestrator/triggers/{id}/run/` | POST | Fire a schedule now, without waiting for the run | Auth (owner) | Light (starts the run, does not wait) | `agents/tests/test_triggers.py::RunNowTests`, `agents/tests/test_scheduler.py` | `TriggerSerializer` | Trigger, ExecutionLog | Goes through `sweep.prepare(manual=True)` — the same paused-agent, overlap and no-goal gates the sweep applies, but no slot claim and no re-arm, so `next_due_at` does not move. Answers **202 with `execution_id`** as soon as the run has started (the old version waited for the whole agent run inside the request); gate words, refusals and start failures answer 200 with the sweep's one-word `outcome`. Records `last_execution` so the card links to the run. Schedule mode only; webhook and disabled triggers answer 400 |
| `/api/orchestrator/triggers/{id}/rotate/` | POST | Issue a new webhook secret, revoking the old URL | Auth (owner) | Light | `agents/tests/test_triggers.py::RotateSecretTests` | `TriggerSerializer` | Trigger | The secret in the path is the **only** credential on the public receiver, so it has to be replaceable: before this, changing a leaked URL meant delete-and-recreate, discarding `last_fired_at`, the failure count and the identity every caller was pointed at. Clears `secret` and re-saves, so `Trigger.save()` mints it — one generator, not two. Instant, with **no grace period**: a leaked credential that keeps working for an hour is a leaked credential. Webhook mode only; anything else answers 400 |
| `/api/orchestrator/hooks/{secret}/` | POST | **Public** webhook receiver — starts an unattended run | **AllowAny** | Heavy |  | — | Trigger, SubAgent, ExecutionLog |  The only unauthenticated route. Secret in the path is the whole credential. Requires `SubAgent.allow_unattended` (enforced again in the runtime, not just here). Body is capped at 64 KB and passed as *context*, never as the goal. Answers 202 with an empty body, and **404 for every refusal** — wrong secret, disabled, and not-cleared must be indistinguishable or it becomes an oracle. A refused or failed firing increments `consecutive_failures` and self-disables at `sweep.MAX_CONSECUTIVE_FAILURES`, like the schedule sweep — until 2026-08-24 only success touched the counter, so a permanently refused hook retried for ever |
| `/api/orchestrator/agents/{id}/reject/` | POST | Decline a paused tool call **and resume the run past it** | Auth (owner) | Heavy |  | `AgentRejectSerializer` (`thread_id`, `call_id`, `reason`) | ExecutionLog, AgentTurn, AgentStep | The mirror of `approve/`, and the fix for an asymmetry that was a bug: approving resumed the run, declining recorded nothing, so a refused call left the run paused for ever. The model is told what was refused (and why, if a `reason` is given) and continues without it. Closes the `HITLRequest` as `rejected`, the mirror of `approve/`. 404 when no paused run holds that thread |

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
| `/api/logs/insights/costs/` | GET | Cost breakdown | Auth | Aggregate |  | `AnalyticsFilterSerializer` (input only) | ExecutionLog, AgentStep, AgentTurn, CostEntry | `by_workflow` rows use the same `workflow_id` / `workflow_name` wire names as everywhere else — never the `subagent__*` column keys. `by_tool` replaced `by_node_type`. Money is `total_cost_usd` / per-row `cost_usd`, **decimal strings** (JSON has no decimal type). `total_credits` is permanently `0`: `credits_used` is a column nothing writes, kept on the wire only so an old client does not read `undefined`. `by_model` says which model the spend went to, which `by_workflow` cannot. A row whose agent has any `unpriced` run reports `cost_source: unpriced` — its sum omits that run, so a figure would understate it while looking exact **Now counts chat** (2026-09-18): `chat` (`messages`, `tokens`, `cost_usd`, `cost_source`, `paid_by` counts), `total_cost_source` for the agent-only total, and `all_cost_usd` / `all_cost_source` for agents + chat. `by_workflow.cost_source` is `billed` when every run was billed (it used to say `estimated` for any priced agent), and deleted agents' runs group under "Deleted agents". **2026-09-21:** `by_kind` (non-token `CostEntry` spend in rupees by kind) and `agents_by_cost_source` (runs carry no payer column, so billed/estimated/unpriced counts are the honest split — chat keeps `paid_by`). Tests: `logs/tests/test_cost.py::InsightsSpendTests`, `logs/tests/test_insights_overview.py` |
| `/api/logs/insights/overview/` | GET | Insights page payload | Auth | Aggregate |  | `AnalyticsFilterSerializer` (input only) | ExecutionLog, AgentStep, AgentTurn, Feedback, RunSignal, HITLRequest, CostEntry | One call joining `stats` + `costs` + `quality` with the three answers none of them give: `tools` (calls + success rate per tool), `delegation` (delegated count, top workers/orchestrators), `agents` (distinct, most_active, most_expensive, needs_attention with min 3 runs, pending_hitl). Excludes `caller='eval'` throughout. **2026-09-21:** `most_expensive` sorts by `cost_usd` (was tokens — wrong across models), unpriced sorts last; tool rows carry `execution_id` + `error` (latest failure), agent rows `example_execution_id` + `example_error`; `agents.median_approve_ms` (median HITL answer time, null when none); `?compare=1` adds `previous` (prior window totals for deltas); 60s per-(user,days,compare) cache, fail-open. Tests: `logs/tests/test_insights_overview.py` |
| `/api/logs/executions/` | GET | Execution history | Auth | O(limit) |  | `ExecutionListFilterSerializer` (input only) | ExecutionLog | Keyset cursor; `count` only on the uncursored first page. `?caller=` filters chat/orchestrator/trigger/api/mission/eval and 400s on an unknown value. **2026-09-21:** `?failure_category=` (provider/step_budget/tool_error/guardrail/contract/timeout/cancelled/interrupted/other, 400 otherwise) for the Insights "What to fix" deep-links. Eval runs hidden unless `caller=eval` is passed explicitly. Each row carries the cost breakdown (`input_tokens` / `output_tokens` / `cached_read_tokens` / `cached_write_tokens`, `cost_usd`, `cost_source`); `input_tokens` **excludes** the cached buckets, so the four are disjoint. Each row also carries `mission_id` (null when the run is not one link in a mission chain), so Activity can badge and group mission runs. A deleted agent's runs are kept and listed as "<name> (deleted)" from their revision snapshot |
| `/api/logs/executions/{id}/` | GET | Run detail | Auth (owner) | O(steps + turns + delegation depth) |  | — | ExecutionLog, AgentTurn, AgentStep, SubAgentRevision | Turns nesting their steps, each turn's full reasoning, the revision used, `delegated_by`, and per-step `delegated_runs`. Steps capped at `EXECUTION_NODE_LOG_LIMIT` and turns at `EXECUTION_TURN_LIMIT` (`steps_truncated` / `turns_truncated`); steps with no turn appear under `unattributed_steps`. A malformed UUID is a 404, not a 500. Cost appears at both levels — per turn (priced against the model *that turn* ran on) and per run (the sum) — plus `cost_usd_total` / `cost_source_total` / `delegated_run_count`, which walk the delegation tree one generation at a time, bounded by `MAX_DELEGATION_DEPTH`. `cost_source` is `billed` (the provider's own charge) \| `estimated` (our price table) \| `unpriced` (no price on record) — and **`unpriced` is not zero**: one unpriced turn makes the whole run unpriced, because a total that silently omits a turn is worse than an admitted gap |
| `/api/logs/agents/{id}/revisions/` | GET | Config change timeline | Auth (owner) | O(revisions) |  | — | SubAgentRevision, ExecutionLog | Newest first, with diffs and per-revision `run_count`. **Keyset paged** on `number` (`?limit=&cursor=`), because the history grows for the life of the agent — the builder shows the newest few, `/agents/:id/history` walks the rest. `limit` is capped at `REVISION_TIMELINE_LIMIT` (400 above it); `count` on the uncursored page only, `has_more`/`next_cursor` drive "show more". 404 for another user's agent |
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
| `/api/inference/documents/` | GET/POST | List/upload documents (`types=csv,xlsx` narrows the caller's own files by `file_type` across the whole tree — the Apps launcher) | Auth | Heavy | ✅ | `DocumentSerializer` | Document, DocumentChunk, IndexedTerm, Folder | **Uploads are allowed by default and refused by exception (2026-09-20)**: `DocumentProcessor.validate_file_upload` blocks native executables (`BLOCKED_MIME_TYPES` / `BLOCKED_EXTENSIONS`) and the size cap, and accepts everything else — an allow-list made the library smaller than the platform (an agent could write a `.xlsx` its owner could not upload). `.xlsx` and `.pptx` now extract text (`extract_xlsx_text`, `extract_pptx_text`); a format with no reader is stored as `file_type='other'` with **no** extracted text (reading unknown bytes as UTF-8 is what indexed zip noise) and is opened later through `run_python_on_files`. Ingestion branches on KB backend: vector/hybrid embed, fulltext chunks only, raw stores text with status `stored`. `file_type` comes from `utils.normalize_file_type(name, mime)` — the one vocabulary, shared with `chat/sources/attachments.py`; never the raw extension. GET takes an optional `folder_id=<id>` or `folder_id=root`; **absent keeps the flat listing**, which is what leaves every existing client working. POST takes `folder_id` in the multipart body (absent = root). A foreign folder gives 404. Uncursored GET capped at 50/list with `truncated`, both lists ordered `-created_at` like the cursor branch; `DocumentSerializer.content` is the full text, so page it. Files are stored at `users/<user_id>/<uuid><ext>` — every segment server-derived, so the physical layout carries no user input and the tree is **not** mirrored on disk. `file_type` now includes `pptx` / `xlsx` (migration `inference.0015`), produced only by the office render tools through `vfs.write_binary`, which also stores the render spec in `metadata.spec` for the preview. The paged (cursor) shape and the folder listing use `DocumentListSerializer`, which **leaves `metadata.spec` out** — it can run to tens of KB per file and no listing renders it; detail (`{id}/`) carries it. The uncursored legacy shape still uses `DocumentSerializer` and carries it alongside `content` |
| `/api/inference/documents/search/` | GET | Search the caller's files by name and contents, with conservative close matches by name (`inference/search.py`) | Auth | O(n) scan | `inference/tests/test_search.py` | `DocumentListSerializer` + `matched_in`/`snippet`/`score` | Document, Folder | `?q=` (under 2 chars answers 200 with empty results, never 400 — the caller is a box being typed into); `?folder_id=` searches that subtree via `filesystem.subtree`, absent/`root` searches the whole tree, foreign id 404s through `resolve_folder`; `?scope=public` searches the shared library flat; `?types=` reuses the listing filter. Two tiers returned separately: `exact` (every query word in name or `content_text`, phrase-in-name first, content hits carry a DB-cut ~160-char plain-text `snippet`) then `fuzzy` (stdlib `difflib` only, every word ≥ 0.75 against some name word, names only — contents are never fuzzy-matched). Ranked, so no cursor: `limit` default 30 / max 50, `truncated` + `note` when capped. Never searches trash (`LiveManager`) or the hidden eval tree |
| `/api/inference/documents/{id}/` | GET/PATCH/DELETE | Document detail; PATCH `{name}` renames (extension must stay, sibling clash 409; leaves `updated_at` — the editors' etag — alone, since a rename is not a content conflict) | Auth (GET: owner or shared into the public library; PATCH/DELETE: owner) | O(1) | ✅ | `DocumentSerializer` | Document, KnowledgeBase, Folder | **DELETE now trashes rather than deletes** — 200 with `purges_after_days`, not 204. It drops the vector index at once (`remove_document_from_kb` + `refresh_kb_stats`) but keeps `content_text` and the file, so restore is a re-ingest through the upload door. `post_delete` does *not* fire on a trash, so the trash path calls the extracted `signals.recount_kb` for `doc_count`; the signal still covers permanent deletes and the two `chat/sources/attachments.py` cleanup paths |
| `/api/inference/documents/{id}/share/` | POST | Share document | Auth | O(1) |  | — | Document | Platform KB stays vector-only by convention. The row is saved **before** the worker thread starts — it re-reads the row for its metadata, so spawning first was a race it could lose. Re-sharing → 403 |
| `/api/inference/pages/` | GET/POST | List the caller's published pages (`?scope=platform`: every listed `platform`/`public` page) / publish one | Auth | O(n), capped | ✅ | — (`inference/pages.py::publish`) | PublishedPage, Document | `inference/tests/test_published_pages.py`. POST and the `publish_page` tool go through the **one** `pages.publish` (kind `report`/`html`/`file`, visibility `link`<`platform`<`public`, default `platform`); a `file` page copies the document's bytes at publish time — a **snapshot, not a pointer**. Capped at `PUBLISHED_PAGE_LIST_LIMIT` with `truncated`. `link` pages never appear in the platform listing |
| `/api/inference/pages/{slug}/` | GET/DELETE | Read one page / withdraw it | Auth | O(1) | ✅ | — | PublishedPage | Signed-in readers reach `link`/`platform`/`public` pages; DELETE is owner-only and **unlists** (`is_listed=False`, `withdrawn_at`) rather than deleting. Every refusal is the same 404 |
| `/api/inference/pages/{slug}/download/` | GET | The snapshot file of a `file` page | Auth | O(1) | | — | PublishedPage | Same visibility rule as the page. `?inline=1` previews (pdf/image) inline; default forces a save |
| `/api/inference/public/pages/` | GET | Public pages, no account | **AllowAny** | O(n), capped | ✅ | — | PublishedPage | Unauthenticated surface: separate anonymous projection (no `is_mine`/`visibility`), CSP header on every response, capped |
| `/api/inference/public/pages/{slug}/` | GET | One public page, no account | **AllowAny** | O(1) | ✅ | — | PublishedPage | Answers only `public` + listed; `link`, `platform`, withdrawn and never-existed are the **same 404** so the route is not an oracle. `html` bodies are returned as data and rendered by `/p/:slug` in an iframe sandboxed without `allow-same-origin`, under a no-network CSP |
| `/api/inference/public/pages/{slug}/download/` | GET | A public page's file, no account | **AllowAny** | O(1) | ✅ | — | PublishedPage | Same 404 rule; CSP header. `?inline=1` previews inline |
| `/api/inference/documents/{id}/download/` | GET | Download original / inline preview | Auth (owner, or shared into the public library) | O(1) | ✅ | — | Document | Streams only if the stored path resolves inside `MEDIA_ROOT` (`views._servable`, reusing `llm/handlers/openai_compatible.validate_attachment_path`), closing the traversal gap the 2026-08-24 audit found. Otherwise it falls back to the extracted text. `?inline=1` serves `Content-Disposition: inline` for in-browser preview (pdf/video/audio/images); default stays `as_attachment` so Export flows keep forcing a save Every response carries `Content-Security-Policy: sandbox` + `nosniff` (`inference/utils.harden_file_response`, S8). The `?token=` query auth is gone (S3); fetch with the header. |
| `/api/inference/documents/{id}/content/` | PATCH | Replace a text document's contents in-browser | Auth (owner) | O(1) | `inference/tests/test_productivity_suite.py` | — | Document | Text types only (`txt|md|csv|json|html`); binaries 400 with a re-render pointer. `If-Match` / `expected_updated_at` must equal the detail's `updated_at` (compared as an instant — `Z` and `+00:00` agree) else 412 with the fresh stamp — an AI draft landing after the human opened it never silently clobbers. Capped at `AGENT_FILE_WRITE_CHARS`. An uploaded file's bytes are rewritten too (`office_edit.save_text`), since download serves the bytes, not `content_text` |
| `/api/inference/documents/new/` | POST | Create a blank file, or a text file with `content`, in `folder_id` (absent = root) — the apps' and file browser's New | Auth | O(1) | `inference/tests/test_office_edit.py` | `DocumentSerializer` | Document, Folder | Extension decides the type (text types + `docx|pptx|xlsx`, which are rendered blank by `chat/tools/office`); a taken name becomes `name (2).ext`; `status='stored'`, never indexed; foreign folder 404 |
| `/api/inference/documents/{id}/office/` | GET/POST | GET: every sheet of an `.xlsx` as raw cells (≤500×40, formulas as source) **plus calculated `values` beside them and the Univer `snapshot`**. POST: `{set_cells, append_rows, sheet, insert_rows, delete_rows, insert_cols, delete_cols, format, freeze, widths}` for a workbook, or `{spec}` for a deck/Word file made here | Auth (GET: owner or shared; POST: owner) | Heavy (openpyxl / re-render) | `inference/tests/test_office_edit.py`, `inference/tests/test_sheets.py` | `DocumentSerializer` | Document | `inference/office_edit.py`. Workbook edits go through `office/edit.apply` (the `edit_workbook` path, same formula refusals) and drop the stale spec; spec edits re-validate + re-render the real file. Structural edits shift formula references in every sheet and move merges; references into a deleted band become `#REF!`. Uploaded `.docx/.pptx` (no spec) 400 — convert through `import/` first. Stale `expected_updated_at` 412. Flushes a pending draft first (`drafts.ensure_rendered`) |
| `/api/inference/documents/{id}/draft/` | POST | Park an office autosave cheaply: `{spec}` for a deck/Word file, `{grid: {sheets}}` or `{snapshot}` for a workbook | Auth (owner) | O(1) | `inference/tests/test_drafts.py` | `DocumentSerializer` | Document | `inference/drafts.py`. Stores the editor state in `metadata.draft` without touching the bytes; the real file is rebuilt after `DRAFT_RENDER_QUIET_SECONDS` (30 s) of quiet by a debounced `spawn()`ed task, or sooner on any read needing the bytes. The render goes through the ordinary edit paths, so one burst keeps one `app` version. Stale `expected_updated_at` 412 — except the render-door: an etag from before the render still lands while nothing else wrote since (`metadata.last_render`, written in the render's own transaction), which any real overwrite clears. The door is in `office_edit.is_stale`, so it holds for `office/`, `import/` and restore too. A render writes only if its draft is still the stored one (row lock), so a draft saved mid-render is never wiped; draft saves are a locked read-modify-write |
| `/api/inference/documents/{id}/copy/` | POST | Duplicate a file into `folder_id` (absent = root) — the file browser's Copy/Paste | Auth (owner) | O(size) | `inference/tests/test_office_edit.py` | `DocumentSerializer` | Document, Folder | Bytes, text and spec copied; `name - Copy.ext` in its own folder, numbered on clash; `stored`, never indexed |
| `/api/inference/documents/{id}/import/` | POST | Convert an uploaded Word/PowerPoint file into an editable spec | Auth (owner) | Heavy (re-render) | `inference/tests/test_import.py` | `DocumentSerializer` | Document, DocumentVersion | Best effort and labelled as a conversion (`converted`, `warnings`); the original upload stays version 1, extracted images are saved beside the file. Refuses files that already have a spec (400). Stale `expected_updated_at` 412 |
| `/api/inference/documents/{id}/asset/` | GET | An image the file's spec embeds (`?path=`) | Auth (owner, or shared into the public library) | O(size) | `inference/tests/test_import.py` | — | Document | Only paths the spec itself names — an editor showing its own figures, not an image proxy — and only raster types (png/jpg/gif/bmp/webp): an SVG would run script on the API origin. Anything else is the same 404. `harden_file_response` |
| `/api/inference/documents/{id}/images/` | POST | Upload an image beside the document, for embedding (multipart `file`) | Auth (owner) | O(size) | `inference/tests/test_import.py` | — | Document | Images only (400 otherwise). Answers the spec path to store; the row is an ordinary image file that happens to sit next to its document |
| `/api/inference/documents/{id}/versions/` | GET | What the file held before each overwrite, newest first | Auth (owner) | O(n) | `inference/tests/test_versions.py` | — | DocumentVersion | Saves from one source within `FILE_VERSION_COALESCE_SECONDS` (5 min) share one version; last `FILE_VERSIONS_KEPT` (25) kept; files over `FILE_VERSION_MAX_BYTES` (25 MB) get no versions. Failing to keep a version never fails the save |
| `/api/inference/documents/{id}/versions/{vid}/download/` | GET | Download one earlier version | Auth (owner) | O(size) | `inference/tests/test_versions.py` | — | DocumentVersion | `?inline=1` previews inline; default forces a save. Foreign or missing version is the same 404 |
| `/api/inference/documents/{id}/versions/{vid}/restore/` | POST | Put a version back (undoable — the replaced state becomes a version) | Auth (owner) | O(size) | `inference/tests/test_versions.py` | `DocumentSerializer` | Document, DocumentVersion | Takes `expected_updated_at` / `If-Match` like a save; stale answers 412. Brings the spec back with the bytes (a version with no spec drops the live one) |
| `/api/inference/documents/{id}/export/` | GET | The file in another format: `?to=pdf\|docx\|md\|txt\|csv\|xlsx` | Auth (owner, or shared into the public library) | Heavy (re-render) | `inference/tests/test_versions.py` | — | Document | Without `to` answers the formats the file offers (`export.FORMATS`), which is what the File menu renders. Not `?format=`: DRF reserves that name and answers 404 before the view runs. `docx→pdf,md,txt`; `md/txt→pdf,docx`; `pptx→pdf` (spec decks only, 409 for uploads); `xlsx→csv`; `csv→xlsx`. Built on a pool thread (slow renders stay off the shared sync thread) that closes its DB connections when done; `harden_file_response` |
| `/api/inference/documents/{id}/preview-image/` | GET | A browser-proof PNG for a TIFF/BMP/HEIC image | Auth (owner, or shared into the public library) | Heavy (Pillow) | `inference/tests/test_previews.py` | — | Document | The download stays the original; converted bytes are capped at 2400 px and cached on the row's metadata. Anything unconvertible (HEIC without pillow-heif) is a 400 with the sentence. `harden_file_response` |
| `/api/inference/documents/{id}/archive/` | GET | The files inside a zip: name, size and date each | Auth (owner, or shared into the public library) | O(n) | `inference/tests/test_previews.py` | — | Document | Entries are listed, never served — no route serves a zip entry's bytes. Capped at 500 with `truncated` |
| `/api/inference/dashboards/` | GET/POST | List/create dashboards (`?scope=platform`: every listed `platform`/`public`) | Auth | O(n), capped | `inference/tests/test_productivity_suite.py` | — (`chat/tools/dashboards._validate_spec`) | Dashboard | Tiles validated through the same `_validate_spec` as `save_dashboard` (kpi/chart/table/text, chart tiles take `render_chart`'s spec). Capped at `PUBLISHED_PAGE_LIST_LIMIT` with `truncated` |
| `/api/inference/dashboards/{id}/` | GET/PATCH/DELETE | Read / tweak / delete one dashboard | Auth | O(1) | `inference/tests/test_productivity_suite.py` | — | Dashboard | Foreign `platform`/`public` rows read 200 but write 404; every other refusal is the same 404 so listings cannot oracle private work. DELETE is a hard delete (owner only) — dashboards have no withdraw state |
| `/api/inference/dashboards/{id}/refresh/` | POST | Refresh a dashboard's sources | Auth | O(1) | `inference/tests/test_productivity_suite.py` | — | Dashboard | Sources do not execute yet — returns the stored spec with `refreshed_at`. The route is stable so `/dashboards` can call it from day one |
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

**Transports (`MCPServer.type`).** `stdio` spawns a subprocess and receives
secrets through `credential_env_map`; `http` and `sse` dial a URL and receive
them through `credential_header_map`. `http` is MCP's **streamable HTTP**
transport, added 2026-09-01 — it is what every hosted connector speaks
(`https://mcp.notion.com/mcp`), and without it such a row could not be created,
let alone connected. `sse` is its deprecated predecessor, kept so existing rows
stay editable; the modal never creates one.

Three things a reviewer should check when touching this:

- **`MCPServer.REMOTE_TYPES` is what the SSRF guard keys on**, in both
  `serializers.py::validate` and `client.py::_prepare_remote`. It was previously
  `== 'sse'`, which meant an `http` row skipped URL validation entirely — a
  user-supplied URL with no guard. A new URL-based transport must join that set.
- **`streamablehttp_client` yields a three-tuple** (`read, write, get_session_id`)
  where `sse_client` yields two, so `_connect_http` cannot be a copy of
  `_connect_sse` with the function swapped.
- **The URL is re-validated at connect time**, not only at registration: DNS for
  a host that passed once can later resolve to a private address.

Both hosted endpoints answer **401** to an unauthenticated handshake, which
`_describe` flattens out of the anyio `ExceptionGroup` into a readable
`connection_failed`. Measured 2026-09-01: `mcp.notion.com` validates a bearer
token against Notion's own API (a bad one returns Notion's native
`"API token is invalid."`), so a static integration token in
`credential_header_map` is a plausible path there; `mcp.slack.com` answers only
the MCP-level `invalid_token` and advertises OAuth resource metadata, so Slack
hosted needs a real OAuth flow. (Update 2026-09-17: that flow now exists —
`mcp_integration/oauth.py`, OAuth 2.1 + dynamic client registration — and stdio
is refused in production by `MCP_ALLOW_STDIO=False`, so a hosted endpoint is
the only path for Slack and Notion; both remain "coming soon".)

| URL | Method | What | Access | Complexity | Tested | Serializer | DB tables | Notes |
|-----|--------|------|--------|-----------|--------|-----------|-----------|-------|
| `/api/mcp/servers/` | GET/POST | List/create MCP servers | Auth (owner) | O(1) |  | `MCPServerSerializer` | MCPServer, MCPServerPreference | `tests_services` (73). **POST refuses `type='native'`** (curated-only, 400 on `type`) and, when `MCP_ALLOW_STDIO=False` (the deployment default), refuses `type='stdio'` too — `test_native_connectors`. Serves presentation metadata (`label`, `category`, `tagline`, `icon_slug`, `help_url`, `coming_soon`) so the catalog is data, not frontend code. **Does not filter on `enabled`** — a platform-disabled row is still listed so the page can render it inert and say why; `coming_soon` splits that state into "Coming soon" vs "Unavailable", and is presentation only (it is always paired with `enabled=False`, which is what actually withholds the tools). One extra query resolves `effective_enabled` for the whole page via `disabled_server_ids` in serializer context. `env` and `credential_file_map` are **write-only** — the latter renders to a file holding a refresh token, and echoing the template back would disclose which credential fields a server receives |
| `/api/mcp/servers/{pk}/` | GET/PUT/PATCH/DELETE | Server CRUD | Auth (owner) | O(1) |  | `MCPServerSerializer` | MCPServer, MCPServerPreference | A PATCH whose body is **exactly** `{enabled}` on a system server writes a preference and returns 200; any other field on a system server ⇒ 403, including `{enabled, command}` together. `tests_connections` |
| `/api/mcp/servers/{pk}/set-enabled/` | POST | Turn a connection on/off for the current user | Auth | O(1) |  | `MCPServerSerializer` | MCPServer, MCPServerPreference | Preferred over PATCH; works uniformly for system (preference row) and owned (row's own `enabled`) servers, so the UI needs one control. Invalidates `MCPToolCache`. Non-boolean ⇒ 400. Enabling a curated row the platform has disabled (`user IS NULL`, `enabled=False`) ⇒ **409** `server_unavailable` with the row's `setup_notes` as `detail`, and no preference row is written — a preference can only ever *subtract*, so storing one there was a 200 for a write that could not take effect. `tests_connections` |
| `/api/mcp/servers/{pk}/tools/` | GET | Capabilities of one server | Auth | External | ✓ | — | MCPServer | `LIST_TOOLS_TIMEOUT` (30s) — `npx -y` installs before the server prints a byte (~8.5s measured), so the old 5s budget timed out on *healthy* connectors too. A `native` row (Gmail, Drive, Sheets, Calendar since `0019`) answers from the tool registry via `native.tools_for_row` — no client, no cache, no timeout. Powers the "What it can do" disclosure, so it deliberately ignores the enable toggle: listing is how a user decides whether to turn a connection on. Every failure carries a `code` and a non-empty `error`: `credential_missing`/`credential_invalid` ⇒ 400, lost access ⇒ 403, `connection_timeout` ⇒ 504, `connection_failed` ⇒ 502 (message includes the child's stderr, e.g. npm's 404). `tests_tool_discovery` |
| `/api/mcp/servers/{pk}/validate_credentials/` | GET | Dry-run credential resolution | Auth | O(1) | ~ | — | MCPServer, Credential | Returns `{ok, errors}`; a resolver that throws is reported as `{ok: false, errors: [reason]}` rather than 500 — a diagnostic that crashes diagnoses nothing. Resolution is a **dry run**: it renders `credential_file_map` but writes nothing to disk, since this endpoint is called per server on every page load. For a `native` row it only checks that the `google-oauth2` credential exists; nothing connects |
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

## 11b. Tool library — app: `tools_config` — [tools_config/views.py](../tools_config/views.py)

Model: `ToolConfig` (one row per user+tool; **absent row = code default**, so a fresh
install has none). The catalogue itself is code — `chat/tools/registry.py` grouped by
`agents/agent/runtime.py:GRANT_TOOLS` — and this app only serves it and lays the user's
overlay on top.

| URL | Method | What | Access | Complexity | Tested | Serializer | DB tables | Notes |
|-----|--------|------|--------|-----------|--------|-----------|-----------|-------|
| `/api/tools/` | GET | Catalogue grouped by grant, with this user's `enabled`/`config` and the `settings` schema for each knob | Auth | O(N tools) | Y | none (hand-built dict) | tools_config_toolconfig (read, cached 60s per user) | No MCP tools — their names are minted at runtime; the `mcp` category carries a note instead |
| `/api/tools/` | PATCH | Switch tools on/off and set their budgets. `{"tool_name": ..., "enabled": ...}` or bulk `{"tools": {name: {enabled, config}}}` | Auth | O(N changed) | Y | `ToolConfigWriteSerializer` | tools_config_toolconfig (write, atomic) | Answers with the whole catalogue. Unknown tool → 400; `LOCKED_TOOLS` (`get_current_time`, `read_tool_output`, `recall_context`) refuse `enabled:false` → 400; unknown config keys dropped and values clamped; a row equal to the defaults is **deleted**, not stored |
| `/api/tools/usage/` | GET | Agents-per-grant counts for the "granted to N agents" chips | Auth | O(agents) | ~ | none | orchestrator_subagent | Reads `tool_grants` only; exposes no agent rows |

Enforcement is not in this app: `chat/tools/__init__.py::disabled_tools_for` is the single
read, consulted by `get_available_tools` (chat), `AgentToolbox.descriptors` (agents) and
`execute_tool` (both, at dispatch — a model can name a tool it was not offered). A failed
read means "nothing is switched off", never "everything is".

The catalogue is the library, not chat's toolbox. Since 2026-09-25 chat (the
orchestrator) holds only reads, infrastructure, delegation, authoring, memory and
missions (`chat_orchestrator_allowed`), and chat's model dispatches through
`execute_chat_tool`; everything else in the catalogue reaches a run only through a
subagent's grants. `/api/chat/execute-tool/` is unchanged (unscoped `execute_tool`
behind its approval refusal).

---

## 11c. Custom tools — app: `datasources` — [datasources/views.py](../datasources/views.py)

Models: `ApiConnection` / `DataConnection` — one row per user-owned tool,
executed through the generic callers (`call_api`, `query_sql`) rather than a
new runtime. **Private by construction**: every queryset filters
`user=request.user`, so a foreign id is 404, never 403. Auth is a vault
reference (`secret_ref` as `type-slug.field`) or nothing — raw secrets are
never accepted, and a slug naming no `CredentialType` is refused at write.
Sharing (snapshots without secrets) lives in `datasources/sharing.py`
(Phase B) — these rows never travel.

| URL | Method | What | Access | Complexity | Tested | Serializer | DB tables | Notes |
|-----|--------|------|--------|-----------|--------|-----------|-----------|-------|
| `/api/datasources/api-connections/` | GET/POST | List / create my HTTP API tools (paginated, name-ordered) | Auth | O(n) | Y | `ApiConnectionSerializer` | datasources_apiconnection | `base_url` must pass the SSRF guard; `auth.type=none` (default) is anonymous, anything else needs a valid `secret_ref`; `openapi_spec` capped at 500 KB; duplicate name per user → 400 |
| `/api/datasources/api-connections/{id}/` | GET/PUT/PATCH/DELETE | Read / replace / edit / delete one of my API tools | Auth | O(1) | Y | same | same | Foreign id → 404 on all four verbs |
| `/api/datasources/data-connections/` | GET/POST | List / create my database tools | Auth | O(n) | Y | `DataConnectionSerializer` | datasources_dataconnection | `sqlite` needs a `/`-rooted `vfs_path`; other kinds need a host passing the SSRF guard; `allow_write` offers `execute_sql`, reads never need it |
| `/api/datasources/data-connections/{id}/` | GET/PUT/PATCH/DELETE | Read / replace / edit / delete one of my database tools | Auth | O(1) | Y | same | same | Foreign id → 404 on all four verbs |

Tests: `datasources/tests/test_api.py` (isolation, validation, anonymity default).

Sharing mirrors agent sharing: `SharedTool` freezes `tool_config` +
`auth_shape` at publish; install writes a private copy owned by the installer
with empty auth. Credentials never travel — not as values, not even as
references; the copy names only the credential *type* to link
(`credentials_needed`). Publishing an agent carries its custom tools as
`api_tool` / `data_tool` requirements with embedded snapshots
(`agents/publishing.py`); installing with `"install"` auto-creates the copies
and maps the new agent's scopes onto them.

| URL | Method | What | Access | Complexity | Tested | Serializer | DB tables | Notes |
|-----|--------|------|--------|-----------|--------|-----------|-----------|-------|
| `/api/datasources/{api\|data}/{id}/share/` | GET | Publish preview: frozen config + auth shape that would travel. Writes nothing | Auth (owner) | O(1) | Y | none | datasources_sharedtool (read) | Foreign id → 404 |
| `/api/datasources/{api\|data}/{id}/share/` | POST | Publish/republish. Body `{tagline, description?, visibility?}` | Auth (owner) | O(1) | Y | none | datasources_sharedtool (write, atomic) | Missing tagline / bad visibility → 400; republish bumps `version`, keeps slug |
| `/api/datasources/{api\|data}/{id}/share/` | DELETE | Withdraw from listing (installs keep working) | Auth (owner) | O(1) | Y | — | same | Idempotent 204 |
| `/api/datasources/shared/` | GET | Listed `platform`/`public` shares + caller's own rows (capped, `truncated` flag) | Auth | O(n) | Y | none | datasources_sharedtool | `link` shares absent by design |
| `/api/datasources/shared/{slug}/` | GET | One shared tool (no `openapi_spec` — card shows `operations_count`; install reads full snapshot server-side) | Auth | O(1) | Y | none | same | Withdrawn/foreign → 404; author always sees own |
| `/api/datasources/shared/{slug}/install/` | POST | Install a private copy + bump `install_count`. Answers `{tool, credentials_needed}` | Auth | O(1) | Y | creation serializers | both connection tables | Copy validated exactly as creation; auth starts empty |

Tests: `datasources/tests/test_sharing.py` (snapshot strips secrets, unauthenticated copies, link/withdraw rules, agent auto-install incl. pointing at an existing connection, vanished-tool publish failure).

---

## 12. Chat (standalone agent + guest) — app: `chat` — [chat/views.py](../chat/views.py), [chat/guest/views.py](../chat/guest/views.py)

Models: `ChatSession`, `ChatMessage`, `ChatAttachment`. Native tool-calling agent
([chat/turn/agent.py](../chat/turn/agent.py)) with a `SENSITIVE_TOOLS` HITL gate. Both send
endpoints run the same pipeline ([chat/turn/pipeline.py](../chat/turn/pipeline.py)) and
differ only in their event sink, so their behaviour cannot drift.

| URL | Method | What | Access | Complexity | Tested | Serializer | DB tables | Notes |
|-----|--------|------|--------|-----------|--------|-----------|-----------|-------|
| `/api/chat/sessions/` | GET/POST | List/create chat session | Auth | O(1) |  | `ChatSessionListSerializer` (GET) / `ChatSessionSerializer` (POST) | ChatSession | **GET carries no `messages`** (2026-09-17): the list nested every transcript, one query per session, to render a sidebar of titles; open a conversation through `{pk}/`, which prefetches `messages__attachments`. Indexed on `(user, -updated_at)`. Paginated (20). Blank title accepted (audit gap 2). Each session carries `total_cost_usd` (a **decimal string**) and `cost_source` — the conversation's running spend, summed from its assistant messages rather than derived from `total_tokens_used`, because the model can change mid-conversation and there is then no single rate to apply. `cost_source` is `''` before the first answer, and one `unpriced` turn makes the whole conversation `unpriced` for good Sessions and messages carry **`paid_by`** (`own_key` | `platform` | `free` | `local` | `mixed` | `''`), resolved by `llm.access.payer` the way `preflight` resolves the key, so the header cost can say whose money it is. A turn's cost now includes the follow-up-questions call and vision-witness calls (`chat/turn/side_calls.py`), and a partly provider-reported turn is estimated rather than labelled `billed` |
| `/api/chat/sessions/{pk}/` | GET/PUT/PATCH/DELETE | Session CRUD | Auth (owner) | O(1) |  | `ChatSessionSerializer` | ChatSession, ChatMessage | `llm_effort` is validated against `llm.effort.LADDER`, not against this session's model: the model can change in the same PATCH, and a level it does not serve is snapped at call time. Blank is valid and means the model's own default — the only way back off the knob. Column defaults moved to `openrouter` / `openrouter/free` / `medium` on 2026-09-03: the NVIDIA catalogue is 410 upstream, so the old default named a model that could not answer. **This makes `OPENROUTER_API_KEY` a required platform key** — without it the picker still offers the free router (`is_free` models are offered before any credential) and the turn fails at preflight. `autonomy` (`ask` | `auto` | `plan`; `full` refused) says how much the turn asks — `auto` lets the reviewer (`chat/turn/reviewer.py`) allow clear matches, `plan` withholds mutating tools. New sessions inherit the account's `default_autonomy`. Sessions and messages carry **`paid_by`** (`own_key` | `platform` | `free` | `local` | `mixed` | `''`), resolved by `llm.access.payer` the way `preflight` resolves the key, so the header cost can say whose money it is. A turn's cost now includes the follow-up-questions call and vision-witness calls (`chat/turn/side_calls.py`), and a partly provider-reported turn is estimated rather than labelled `billed` |
| `/api/chat/sessions/{sid}/message/` | POST | Send message, wait for full reply | Auth | External |  | — | ChatMessage, ChatSession | Same pipeline as the stream, events discarded. The assistant row records its own cost (`model_id`, the four token buckets, `cost_usd`, `cost_source`), priced against the model that answered *that* turn — switching the session's model must not retroactively reprice earlier answers |
| `/api/chat/sessions/{sid}/message/steer/` | POST | Say something to a chat turn already running | Auth (owner) | Light |  | — (JSON `message`) | — (in-process mailbox) | Not a second turn: two turns on one session interleave into one transcript. 404 when no turn is running, so "delivered" and "went nowhere" cannot look alike. Queued rather than last-write-wins; response carries `delivered` / `queued` / `dropped`. Body also takes `autonomy` (`ask` \| `auto` \| `review`) — a mid-run mode switch riding the same mailbox, standing for the rest of the run; needs no running turn (applies at the next tool boundary, else next turn). `plan` is refused mid-run: the toolbox is already built. Tests: `chat/tests/test_steering.py`, `chat/tests/test_auto_mode.py` |
| `/api/chat/sessions/{sid}/message/stream/` | POST | Send message (SSE stream) | Auth | Stream/External |  | — | ChatMessage, ChatSession, ToolOutput, ToolPermission | Body also takes `approve_tool_call`, `approval_scope` (`once` \| `session` \| `always`) and the retired `remember_approval` (equivalent to `always`) — see `chat/tools/permissions.py`. A `session`-scoped allowance is keyed on the chat session id, **not** the thread id: with memory off the thread is a throwaway `<id>:nomem:<uuid>` and the row would match nothing. Streams `content_chunk` token-by-token; auth is manual (DRF cannot wrap `StreamingHttpResponse`). The turn is owned by `chat/turn/runs.py`, **not** by this response — dropping the connection does not cancel it. A send while the session is already answering attaches to the running turn instead of starting a second one. Body also takes `llm_effort`, and its three values are not interchangeable: **absent** means the session's stored level stands (so a client predating the field is unaffected), `""` is an explicit request for the model's default and *clears* a stored level, and a level name is itself. An unrecognised level reads as absent rather than failing the turn. **Also takes `reject_tool_call` + `reject_reason`** (2026-09-05), the mirror of the approval and a complete request on its own — a refusal carries no new text, so requiring `content` would mean inventing a message on the user's behalf. It existed at the graph level (`reject_tool_call`) from the day agent runs got the same asymmetry fixed; chat's Deny button called `clearPendingToolCall()` and nothing else, so the card vanished, the graph stayed parked and the model was never told. The `ask_permission` frame now carries `detail` — `chat/tools/describe.py` — alongside the raw `args`, which the card keeps behind a closed disclosure. **Also takes `command` (`{name, args, text}`, P10 §18)** — a slash command as structured input (chips carry ids, so a chosen name is never re-parsed). A typed `/line` is parsed by the same code; anything that fails to parse or resolve is a 400 under the input, never a model turn. Turn commands resolve to a trailing context message (never the system prompt — the prefix-cache guard); `/agent` starts one run with `caller='chat'` through the one door before the chat turn, and the command is stored on both messages (`metadata.command`) so the transcript shows a chip and regenerate replays it. Tests: `chat/tests/test_commands.py`. **Also takes `answer_tool_call` + `answer`** (2026-09-25): the answer to an `ask_user` question card, recorded with `answer_question` (checked against the question in the checkpoint; a misfit is a 400 before anything streams) and resumed like an approval. The paused turn emits `ask_question` (`call_id`, `question`, `kind`, `options`, `allow_other`, `min`/`max`/`step`/`unit`, `assumption`); a skip is `reject_tool_call`. Tests: `chat/tests/test_questions.py` |
| `/api/chat/sessions/{sid}/message/attach/` | POST | Re-attach to a running turn (SSE) | Auth (run owner) | Stream |  | — | — | Replays every buffered frame, then follows live; body `{from: n}` skips frames the caller has. Closes with no frames when no run exists (answer is already in the DB). Read-only: never starts work |
| `/api/chat/sessions/{sid}/message/stop/` | POST | Stop the running turn | Auth (run owner) | O(1) |  | — | ChatMessage | The **only** thing that cancels a turn. Persists whatever streamed as an answer with `metadata.interrupted`; emits a closing `done` to everyone attached. 404 when nothing is running |
| `/api/chat/runs/` | GET | Session ids with a turn still running | Auth | O(n) in live runs |  | — | — | In-memory, scoped to the caller. Drives re-attach after a reload and the "still working" marker. Per-process — see the multi-worker note in `chat/turn/runs.py` |
| `/api/chat/sessions/{sid}/messages/{mid}/` | DELETE | Delete a message | Auth (owner) | O(1) | ~ | — | ChatMessage | |
| `/api/chat/sessions/{sid}/upload/` | POST | Upload attachment | Auth | O(1) | ~ | — | ChatAttachment | Multipart |
| `/api/chat/execute-tool/` | POST | Execute a chat tool | Auth | External |  | — | MCPServer, ToolPermission | Non-object `args` → 400; **403 whenever the agent loop would have paused** — `SENSITIVE_TOOLS` *or* `chat.permissions.default_policy`, so a credentialed MCP write cannot be run here to dodge the gate |
| `/api/chat/guest/sessions/` | POST | Create guest session | **Public** (IP-limited) | O(1) | ~ | — | ChatSession | Pinned to NVIDIA NIM + `nvidia/nemotron-3.5-lightning-30b-a3b`; a requested provider/model is ignored |
| `/api/chat/guest/sessions/{sid}/` | GET | Get guest session | **Public** | O(1) | ~ | — | ChatSession, ChatMessage | |
| `/api/chat/guest/sessions/{sid}/message/stream/` | POST | Guest stream message | **Public** (IP-limited) | Stream/External | ~ | — | ChatMessage | Re-pins the session row to the guest provider/model before answering |
| `/api/chat/commands/` | GET | Slash commands this user can run | Auth | O(n) | `chat/tests/test_commands.py` | — | — (registry + engine gates) | Filtered like tools: an engine at `none` hides the command rather than showing one that refuses; guests see only `guest=True` ones. Capped at 100, and the body says when truncated |
| `/api/chat/commands/complete/` | GET | Candidates for one command argument | Auth | O(n) | `chat/tests/test_commands.py` | — (`command`, `arg`, `q`) | SubAgent, Skill, AIModel, CodeProject, MCPServer, Mission, VFS | Computed with the same predicate validation uses (the template rule); capped at 20, substring match, prefix first |
| `/api/chat/commands/run/` | POST | Run an action-kind command, return its card | Auth | O(1)–O(n) | `chat/tests/test_commands.py` | — (`name`, `args`, `chips`, `confirm`) | UserMemory, Mission, ExecutionLog, CostEntry, ChatMessage | No model call. Turn/client kinds are refused here (turns run through the message endpoints, clients in the browser). `/memory <fact>` writes through `core/memory.py`, so dedup/caps still apply; `/memory forget` never deletes on an unseen fuzzy match |
| `/api/chat/commands/confirm/` | POST | Carry out a confirm-sheet decision | Auth | O(1) | `chat/tests/test_commands.py` | — (`name`, `args`, `confirm`) | Mission, Trigger, UserMemory | `goal` (mission start, 201), `schedule` (trigger arm, 201), `memory_forget` (delete by id). Pressing Start is the approval, with the same ownership checks as every other write path |

---

## 13. Buddy — BrowserOS desktop assistant — **REMOVED 2026-09-02, to be rebuilt**

The `buddy` backend app (`api/buddy/commands/`) was removed again on 2026-09-02.
It had been briefly restored 2026-09-01 as a single stateless command endpoint,
but the desktop assistant is being redesigned against the agent model rather
than kept as a bespoke one-shot decision endpoint. The **BrowserOS frontend was
deliberately kept** — `components/os/BuddyPanel.tsx` and
`components/apps/ChatbotApp.tsx` still POST `/api/buddy/commands/`, so those
calls 404 until the backend is rebuilt. No models or sockets were involved, so
removal was a clean deletion (app package + `INSTALLED_APPS` + URL include); no
migration.

### 14. BrowserOS workspace models — **REMOVED 2026-08-16, not restored**

`api/browseros/` and the `ws/buddy/` socket stay gone. `OSWorkspace` /
`OSAppWindow` persisted desktop layout server-side; BrowserOS keeps that in its
own contexts. Chat also lost the screen-context block `ws/buddy/` warmed — the
`screen_context` field the chat POST body carried was never read by anything.

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

Models: `Notification`, `NotificationPreference`, `HITLReminderSchedule`,
`PushSubscription`, `ScheduledNotification`. Router
ViewSet (full CRUD) plus non-router routes declared *before* the router — its
`''` registration is greedy and would otherwise capture them as detail lookups.

| URL | Method | What | Access | Complexity | Tested | Serializer | DB tables | Notes |
|-----|--------|------|--------|-----------|--------|-----------|-----------|-------|
| `/api/notifications/` | GET/POST | List/create notification | Auth | O(1) | ~ | `NotificationSerializer` | Notification |  POST → IntegrityError 500 (audit Bug 3) — `perform_create` misses `user` |
| `/api/notifications/{pk}/` | GET/PUT/PATCH/DELETE | Notification CRUD | Auth (owner) | O(1) | ~ | `NotificationSerializer` | Notification | |
| `/api/notifications/{pk}/mark_read/` (action) | POST | Mark read | Auth (owner) | O(1) | ~ | — | Notification | |
| `/api/notifications/mark_all_read/` (action) | POST | Bulk mark read | Auth (owner) | O(n) | ~ | — | Notification | |
| `/api/notifications/preferences/` | GET/PUT/PATCH | HITL reminder delivery rules | Auth (self) | O(1) |  | `NotificationPreferenceSerializer` | NotificationPreference | get_or_create on read; `last_digest_sent_on`/`last_hourly_sent_at` are read-only — they are the once-per-day email cap |
| `/api/notifications/hitl-reminders/` | GET | Caller's armed escalation ladders | Auth (owner) | O(n) | ~ | `HITLReminderScheduleSerializer` | HITLReminderSchedule, HITLRequest | Read-only; diagnostics for "why did/didn't I get nudged" |
| `/api/notifications/push/vapid-key/` | GET | VAPID public key + whether closed-browser push is live | Auth | O(1) | Yes | — | — | Blank keys = push off; sockets/toasts/digest keep working |
| `/api/notifications/push/` | GET | Caller's subscribed browsers | Auth (owner) | O(n) | Yes | `PushSubscriptionSerializer` | PushSubscription | |
| `/api/notifications/push/subscribe/` | POST | Store/refresh one browser subscription (upsert on endpoint) | Auth | O(1) | Yes | `PushSubscriptionSerializer` | PushSubscription | Re-login on a shared machine re-owns the endpoint |
| `/api/notifications/push/unsubscribe/` | POST | Remove one browser subscription (unknown endpoint still 200) | Auth | O(1) | Yes | — | PushSubscription | |
| `/api/notifications/scheduled/` | GET | Caller's live reminders (ordered, capped at the 20-live limit) | Auth | O(n) | Y | `ScheduledNotificationSerializer` (read-only) | ScheduledNotification | Creation stays in chat — the tool quotes the user's timing; no second write path |
| `/api/notifications/scheduled/{id}/` | DELETE | Cancel one live reminder (204; a repeat call returns 404, not a second 204 — foreign id → 404, never 403) | Auth (owner) | O(1) | Y | — | ScheduledNotification | Spent rows answer 404, not a state error |

**Reminder delivery** (`notifications/reminders.py`, swept by
`notifications.sweep_hitl_reminders` on Celery beat every
`HITL_REMINDER_SWEEP_SECONDS`, default 300, or by
`manage.py send_hitl_reminders`):

| Channel | Trigger | Transport |
|---------|---------|-----------|
| Escalation | +0 / +1h / +1d after an unanswered `HITLRequest`, then stops | Device push (`ws/hitl/` + Web Push) + in-app row. **Never email.** |
| Hourly | Opt-in; once an hour while anything is pending | Device push (`ws/hitl/` + Web Push) + in-app row. **Never email.** |
| Daily digest | User's chosen local wall-clock time | Email + in-app row + device push (`ws/hitl/` + Web Push). Capped at one per calendar day. |
| Agent update | `notify_user` tool / e-sign events | Socket + Web Push + in-app row. Max 3 per run. **Never email.** |
| Scheduled reminder | `schedule_notification` tool, fired by `notifications.sweep_scheduled` on beat every `SCHEDULED_SWEEP_SECONDS` (default 60) or `manage.py send_scheduled_notifications` | In-app row always + device ping unless quiet hours + Web Push twin. Email only per-reminder opt-in (`email: true`, default off — explicit user consent is what separates it from system nudges). One-shot spends after firing; `hourly`/`daily`/`weekly` advance from the due time. Max 20 live per user. |

**Run visibility + reminder tools** (`chat/tools/runs.py`,
`chat/tools/workspace.py`, `chat/tools/agents.py::get_agent_run`): `list_user_runs`
(status default `running`, limit default 10/max 25, user-scoped, compact rows —
goal, turns/steps, todo/task progress, approval flag, cost, `/runs` link) and
the progress block on `get_agent_run` (same shape, no trace payloads).
Progress names its source per half: `output_data` todos/tasks/files exist at
run close (`run record`); a running run's plan is read best-effort from its
own checkpointer (`live`, bounded rows and seconds, miss degrades to turn
activity rather than failing). All four reminder-adjacent tools
(`list_user_runs`, `schedule/list/cancel_scheduled_notification`) are
`ALWAYS_AVAILABLE` — they touch only the caller's own rows. Pending reminders
are managed on the Notifications tab (`ScheduledReminders.tsx`) over
`GET/DELETE /api/notifications/scheduled/`. Tests:
`chat/tests/test_run_visibility.py`, `chat/tests/test_reminder_tools.py`,
`notifications/tests/test_scheduled_sweep.py`,
`notifications/tests/test_scheduled_api.py`.

---

## 17. Imagine (media generation) — app: `imagine` — [imagine/views.py](../imagine/views.py)

Models: `Generation`, `ImagineConversation`, `ImagineMessage`. Router ViewSets + agent APIViews.

| URL | Method | What | Access | Complexity | Tested | Serializer | DB tables | Notes |
|-----|--------|------|--------|-----------|--------|-----------|-----------|-------|
| `/api/imagine/capabilities/` | GET | Model catalog per modality + `defaults`/`recommended` | Auth | External (cached 1h) |  tests_api | — | — | `?refresh=1` bypasses the cache (serialised by a lock so concurrent refreshes cannot stampede; the loser serves the cached copy). 400 + `detail` **and `code: credential_missing`** when the user has no OpenRouter credential — the client keys on the code, since "every 400 here means no key" was true only while nothing else answered 400. The message is one sentence naming one place (`openrouter.MISSING_CREDENTIAL_MESSAGE`); it used to append this app's advice to the resolver's, which already ended in "Add one under Credentials". Image/video come from OpenRouter's `/images/models` and `/videos/models`; audio is curated in `services/catalog.TTS_MODELS` (no TTS discovery endpoint exists). **Each model's descriptor is now its own advertised dial set** and nothing is invented: `resolutions`, `aspect_ratios`, `sizes`, `qualities`, `output_formats`, `backgrounds`, `output_compression` {min,max}, `batch` {min,max}, `max_references`, `durations`, `frame_slots`, `supports_audio|seed|speed|instructions`, `speed_range`, `response_formats`. An empty list means *this model takes no such dial*, and the panel renders no control — normalization used to substitute `["1K","2K"]` where a model advertised none, which is a hard 400 on a model whose tiers are 2K/4K (`resolution "512": not supported. Accepted: 2K, 4K`). The one inversion is `voices`: empty there means a free-form provider voice id. Tests: `imagine/tests/test_catalog.py` |
| `/api/imagine/agent/chat/` | POST | Media-gen agent chat | Auth | External | — | — | ImagineConversation, ImagineMessage | Optional `model` pins the model **and** the modality (see `intent.classify(preferred_model=…)`). Carries the `imagine_generate` cost throttle. **Preflights the OpenRouter credential** and answers 400 + `code: credential_missing` when it is absent, creating no conversation: it used to answer 200 with an assistant *message* apologising for something only the user can fix, which is the studio's version of looking busy before failing. `agent/resume/` does the same for every decision but `cancel` — clearing a pending intent needs no provider, and refusing it would strand the conversation in `awaiting_hitl` The router's proposed params go through the *same* dial table as the form path, with the other policy: `validation.constrain` **drops** what the model will not take instead of refusing the turn, since a router that guessed `quality: high` has not made the request impossible. Two policies, one table — a second copy would go stale on the next model OpenRouter ships |
| `/api/imagine/agent/resume/` | POST | Resume agent run (after HITL) | Auth | External | — | — | ImagineConversation | Carries the `imagine_generate` cost throttle |
| `/api/imagine/conversations/` | GET | Conversation list | Auth (owner) | O(1) | — | `ImagineConversationSerializer` | ImagineConversation, ImagineMessage | Read-only viewset despite the router's default verbs |
| `/api/imagine/conversations/{pk}/` | GET | Conversation detail + messages | Auth (owner) | O(n messages) | — | `ImagineConversationDetailSerializer` | ImagineConversation, ImagineMessage | |
| `/api/imagine/` | GET/POST | Generation list/create | Auth (owner) | External |  tests_api | `GenerationSerializer` | Generation | POST create is preflighted: no OpenRouter credential → 400 + `detail`, no row created. Image dispatch is async only under `RUN_WORKFLOWS_ASYNC` (row returns `pending`, worker broadcasts completion; falls back to inline if the broker is unreachable), otherwise image/audio block in the request cycle (up to 120 s); video always returns `pending` and is polled by Celery. Create carries the `imagine_generate` cost throttle. `validate()` rejects a model the modality cannot run. `metadata` is read-only **The wire now carries the complete dial set both endpoints accept** — image: `resolution`, `aspect_ratio`, `quality`, `output_format`, `background`, `output_compression`, `batch_size` (`n`), `seed`, `reference_urls` (`input_references`); video: adds `size` and `frame_images` (`first_frame`/`last_frame`, i.e. image-to-video); audio: `voice`, `speed`, `response_format`, `instructions`. Every one is validated against the *selected model's* advertised descriptor by `imagine/validation.py::validate_dials` **before** a billed call, because the two ways of getting it wrong do not look alike: an out-of-enum value is a provider 400, while a dial the model never advertised is accepted and silently ignored — the request succeeds, the user is billed, and the setting did nothing. `batch_size` > 1 returns several images in `output_urls` (read-only; `output_url` stays the first). Tests: `imagine/tests/test_dials.py` |
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
verdict), `EvalWorld` (one fake situation a suite's cases share: brief,
surfaces, fixtures, planted facts). Design notes: [EVALUATION.md](EVALUATION.md),
[EVAL_ENVIRONMENTS_PLAN.md](EVAL_ENVIRONMENTS_PLAN.md).

Views are thin sync `@api_view`s; reads live in `eval/queries.py`. Only
`suite_run` is async (adrf), because it preflights the provider before
answering. Every route is scoped to `request.user` through the query layer — a
suite, case, run or result belonging to someone else is a **404**, never a 403.

**A UI landed 2026-09-01** (`pages/Evals.tsx`, `api/evals.ts`, route `/evals`).
Until then this was the one app with a complete backend and no caller in the
frontend at all — the feature existed and was unreachable. Two contract details
the page had to be corrected on, worth knowing before writing another client:
the review body takes `verdict: 'pass' | 'fail' | 'unsure'` (a **choice, not a
boolean** — `unsure` is a real third answer, because forcing a coin-flip would
corrupt the `grader_agreement` the feature exists to produce) alongside
`comment` / `corrected_answer`; and `EvalRun.passed` is `null` while `status` is
`awaiting_review`, which is a provisional score rather than a missing one and
must not be rendered as a failure.

A sweep runs the agent once per case through the same `run_agent` door as every
other run, so a suite's size is a bill: `EVAL_MAX_CASES_PER_SUITE` (200) and
`EVAL_MAX_CONCURRENCY` (4) are the bounds, and list responses carry their own
caps (`EVAL_RUN_LIST_LIMIT`, `EVAL_RESULT_LIST_LIMIT`, `EVAL_REVIEW_QUEUE_LIMIT`)
because DRF pagination never reaches `@api_view` functions.

| URL | Method | What | Access | Complexity | Tested | Serializer | DB tables | Notes |
|-----|--------|------|--------|-----------|--------|-----------|-----------|-------|
| `/api/eval/graders/` | GET | Grader catalog | Auth | O(1) |  | — | — | Served from `graders.REGISTRY`, the same dict the runner dispatches through — a picker can never offer a grader nothing implements |
| `/api/eval/suites/` | GET/POST | List / create suites | Auth (owner) | O(suites) |  | `EvalSuiteSerializer` | EvalSuite, EvalCase, EvalRun | GET also returns `health` (per-suite case/run/pending-review counts). POST 400s on another user's `subagent`, an unknown `supervision`, `pass_threshold` outside [0,1], or `concurrency` above the cap |
| `/api/eval/suites/{id}/` | GET/PATCH/DELETE | One suite (+ its cases) | Auth (owner) | O(cases + runs) | `eval/tests/test_api.py` | `EvalSuiteSerializer`, `EvalCaseSerializer` | EvalSuite, EvalCase, EvalRun, EvalResult, ExecutionLog, Document, Folder, KnowledgeBase | 404 for another user's suite. DELETE removes its sweeps, eval-only traces, hidden attempt files and world KBs; 409 while a sweep is still active. `gated_calls` (`run`\|`block`): what an eval does with a call that would have paused — recorded as an intent either way, then run or declined. Benchmark guardrail suites install as `block` |
| `/api/eval/suites/{id}/cases/` | GET/POST | List / add cases | Auth (owner) | O(cases) |  | `EvalCaseSerializer` | EvalCase | **Graders validated on write** against `graders.REGISTRY`: unknown type, missing required param, unknown param or non-positive weight → 400. A case with only an LLM judge is refused — pair it with one deterministic check. Capped at `EVAL_MAX_CASES_PER_SUITE` |
| `/api/eval/suites/{id}/generate/` | POST | Draft cases from the suite agent's config | Auth (owner) | External (1 judge call) |  | `EvalCaseSerializer` | EvalSuite, EvalCase, SubAgent | `{count?≤25, focus?}` → 201 `{cases, rejected, tokens, cost_usd, model}`. The **judge model** writes them (`eval/generator.py`), billed to the caller. Saved as **drafts** (`is_active=False`, tag `needs-review`) the runner skips. Graders limited to `GENERATABLE_GRADERS` (no fixtures); a case naming a tool the agent lacks is rejected with a reason, not saved. 400 no agent on the suite; 400 when the suite has an accepted world (generate from the world instead); 402 judge credential missing; 502 unusable reply. Capped by `EVAL_MAX_CASES_PER_SUITE` |
| `/api/eval/suites/{id}/world/` | GET | Live, draft and pending world | Auth (owner) | O(1) |  | `EvalWorldSerializer` | EvalWorld | `{live, draft, pending, versions}` — the accepted world sweeps run on, the newest draft awaiting review, a generation still running (`generating`) or the newest one that `failed` (with `error_message`; only while nothing newer superseded it), and every version number. The page polls this while `pending.status == 'generating'` |
| `/api/eval/suites/{id}/world/generate/` | POST | Start judge-building a world + its cases | Auth (owner) | O(1) now; External (~5 judge calls) in the background |  | `EvalWorldSerializer` | EvalWorld, EvalCase, Notification | `{focus?, cases?≤25}` → **202** `{world}` with `status='generating'`; a detached task (`eval/api.py::start_world_generation`) runs facts → fixtures → coverage check → cases → blind solve → blind verify (both matched **by case id**) → prove, then the row becomes `draft` (+ draft cases) or `failed` (+ `error_message`) and the owner is notified (`action_url=/evals`). Refused up front: **400** agent has nothing a world can hold (surfaces honour its connector scope; files are optional), **402** no judge credential, **409** a generation is already running. Stale `generating` rows (>30 min) are failed by `eval/recovery.py` |
| `/api/eval/worlds/{id}/accept/` | POST | Accept a draft world | Auth (owner) | O(cases) |  | `EvalWorldSerializer` | EvalWorld, KnowledgeBase | Its cases become acceptable; older versions' cases go stale (kept, never swept) and their hidden KBs are dropped. 400 if already accepted |
| `/api/eval/worlds/{id}/` | GET/DELETE | One world | Auth (owner) | O(1) |  | `EvalWorldSerializer` | EvalWorld | Draft and failed worlds may be deleted; a `generating` one is **409**; accepted worlds are history and refuse — regenerate instead |
| `/api/eval/suites/{id}/import-runs/` | POST | Draft cases from the agent's real runs | Auth (owner) | O(runs≤100) |  | `EvalCaseSerializer` | EvalSuite, EvalCase, ExecutionLog, Feedback | `{source?: all\|rated\|thumbs_down, limit?≤50}` → 201 `{cases, already_imported}`. Newest first; `caller='eval'` runs excluded; a run already in the suite (by execution id tag) is skipped. Thumbs-down comment → rubric; thumbs-up → accepted answer is the bar. Drafts, as above |
| `/api/eval/suites/{id}/drafts/` | POST | Accept / reject drafts | Auth (owner) | O(drafts) |  | inline | EvalCase | `{accept: [ids], reject: [ids]}` → `{accepted, rejected, refused?, refused_reason?}`. Acts **only on drafts of this suite** (inactive + `needs-review`); any other id is ignored, so a hand-written case can never be deleted here. Accept wins over reject for the same id. A case built for a world version that is not accepted is **refused**, not accepted — accept the world first |
| `/api/eval/starter-kits/` | GET | Starter datasets to clone | Auth | O(1) |  | — | — | `?agent_id=` adds `recommended` from that agent's grants. No rows created |
| `/api/eval/suites/from-template/` | POST | Clone a starter kit | Auth (owner) | O(cases) |  | — | EvalSuite, EvalCase | `{template, name?, agent_id?}` → suite (`template_slug` set) + 5 cases. 400 on unknown kit or another user's agent |
| `/api/eval/cases/{id}/` | GET/PATCH/DELETE | One case | Auth (owner) | O(1) |  | `EvalCaseSerializer` | EvalCase | DELETE keeps the results that scored it — `EvalResult.case` is SET_NULL with `case_name`/`goal` copied |
| `/api/eval/suites/{id}/run/` | POST | Sweep the suite | Auth (owner) | External × cases |  | `RunRequestSerializer` | EvalRun, EvalResult, ExecutionLog | **202 + `run_id`**, detached via `background.spawn()`. Guardrails + provider preflight happen first: spent cap or missing credential → **402**, retired model/unknown provider → **400**, no active cases → 400, no agent named → 400. `agent_id` falls back to the suite's `subagent`; another user's agent is a 404. Only runnable cases sweep: active ones built for the live world version (stale cases skip; all-stale reads as no cases, with the reason). The run records the world `version` it ran on |
| `/api/eval/runs/` | GET | Sweep history | Auth (owner) | O(limit) |  | `RunListFilterSerializer` (input), `EvalRunSerializer` | EvalRun | `?suite_id`/`?agent_id`/`?status`; unknown status → 400. Body carries `count` + `truncated` |
| `/api/eval/runs/{run_id}/` | GET/DELETE | Read a sweep + results, or delete a finished sweep | Auth (owner) | O(results) | `eval/tests/test_api.py` | `EvalRunSerializer`, `EvalResultSerializer` | EvalRun, EvalResult, EvalReview, ExecutionLog, Document, Folder | GET carries grades, reviews, scores, flags, intents, world changes and execution trace ids; capped at `EVAL_RESULT_LIST_LIMIT`. DELETE removes the run, results/reviews, caller-owned `caller='eval'` traces, and its hidden attempt folders; 409 while pending/running or while cancellation is still settling. A malformed UUID is a 404 |
| `/api/orchestrator/agents/wizard/questions/` | POST | Creation wizard questions | Auth | O(1) |  | — | — | Read-only: `{description}` → questions + memory hints. No rows written |
| `/api/orchestrator/agents/wizard/propose/` | POST | Creation wizard proposal | Auth | O(1) |  | — | — | Read-only: `{description, answers}` → proposed AgentConfig + explanations + warnings. Creation goes through POST agents |
| `/api/eval/runs/{run_id}/cancel/` | POST | Stop a sweep | Auth (owner) | O(1) |  | — | EvalRun | Cooperative — cases check the row on the way out of the concurrency semaphore; one already inside a model call finishes. 400 if the run already ended |
| `/api/eval/reviews/pending/` | GET | The review queue | Auth (suite reviewer or owner) | O(limit) |  | `QueueFilterSerializer` (input), `EvalResultSerializer` | EvalResult, EvalRun, EvalSuite, EvalCase | **Oldest first** — a queue is worked through. Each row carries the case `reference` so a reviewer is not deciding without the rubric |
| `/api/eval/results/{id}/review/` | POST | Record a verdict or dismiss an errored result | Auth (suite reviewer or owner) | O(results in run) | `eval/tests/test_api.py` | `ReviewInputSerializer` | EvalReview, EvalResult, EvalRun, ExecutionLog, Document, Folder | `pass`/`fail`/`unsure`. Normal verdicts override graders without overwriting `auto_passed`. Legacy `error`/`skipped` rows have no answer to judge: the queue shows a Dismiss action (`unsure`), which deletes that result, its eval-only trace and hidden attempt files instead of returning 400. New errors/skips are never queued. Re-settles the run after dismissal |
| `/api/eval/agents/{id}/scorecard/` | GET | Scores per suite over time | Auth (owner) | O(runs≤200) |  | — | EvalRun, EvalSuite | `latest` is null while a suite's newest sweep is still `awaiting_review` — a provisional score must not read as the current one. Each point names the `revision` it was scored under |
| `/api/eval/judge/calibration/` | GET | Latest judge calibration | Auth | O(1) |  | — | JudgeCalibration | Platform-wide, read-only. Latest per source (`handwritten`, `gold`) with agreement / false-pass / false-fail |
| `/api/eval/cases/from-run/` | POST | Save a run as a case draft | Auth (owner) | O(1) |  | inline | EvalSuite, EvalCase, ExecutionLog | `{execution_id, suite_id?}` + `{draft: true}`. 404 for another user's run; 400 for a chat turn (no agent), or a full suite. Creates "From runs" (`supervision='all'`) on first use. `graders=[]`, tags carry the execution id; files only recorded as paths (v1 limit). Draft: the goal and reference are the run's own words, so it scores nothing until accepted |
| `/api/logs/feedback/` | PUT/DELETE | Thumbs up/down | Auth (owner) | O(1) |  | inline | Feedback | Upsert on PUT `{target, id, rating, reason?, comment?}`; clear on DELETE. Every refusal is 404 (no ownership oracle) |
| `/api/logs/insights/quality/` | GET | Quality counts | Auth | Aggregate |  | `AnalyticsFilterSerializer` | ExecutionLog, Feedback, RunSignal | `?days=30`. Failures by category, thumbs totals/by-reason, signals by kind, 20 recent thumbs-down. Excludes `caller='eval'` |

Tests: `eval/tests/` — `test_graders.py`, `test_supervision.py`,
`test_runner.py`, `test_api.py`, `test_public_api.py`, `test_recovery.py`,
`test_phase1.py`, `test_phases.py`, `test_calibration.py`,
`test_case_from_run.py`, `test_external_adapters.py`, `test_expansion.py`
(new graders, flags, starter kits, wizard),
`test_environments.py` (E-1: worlds, confinement, withholding, pipeline),
`test_kb_worlds.py` (E-2: hidden KB, `cited`),
`test_sim_worlds.py` (E-3: mail/calendar sims, `env_*` graders),
`test_drive_web_worlds.py` (E-4: drive/web sims, graders),
`test_eval_chat.py` (E-5: chat tools, `save_cases`, `/eval` drafts);
`logs/tests/test_feedback.py`, `test_run_signals.py`,
`test_failure_category.py`, `test_eval_exclusion.py`;
`agents/tests/test_eval_caller.py`.

**Importing this app elsewhere:** `eval/api.py` is the public surface — pure
grading (`grade_answer`, `grade_execution`, `list_graders`, `needs_review`),
sweeps (`run_suite_now` awaited, `start_suite_run` detached), supervision
(`record_review`) and the reads. No module-level import of a sibling app, so no
cycle is possible; `eval/__init__.py` stays empty because `INSTALLED_APPS`
imports the package before the app registry is ready. See
[EVALUATION.md](EVALUATION.md) §6.

---

## 21. Workspaces — compute plane + Code tab — app: `workspaces` — [workspaces/engine.py](../workspaces/engine.py), [workspaces/models.py](../workspaces/models.py)

Models: `Workspace` (one per user: provider id, status, disk, egress policy,
webhook secret), `WorkspaceJob` (one detached command: status, exit code, log
path, timeout), `CodeProject` (one repo: name, repo url, branch, workspace
path, GitHub secret ref), `CodeChange` (one file a run changed: path, hashes,
diff). Design note: [PLATFORM_CAPABILITIES_PLAN.md](PLATFORM_CAPABILITIES_PLAN.md)
§8 (P5) + §9 (P6).

One-door engine (`WORKSPACE_ENGINE=none|docker|<provider>`, default `none`):
with `none` the `compute` and `shell` tools are not offered, never
offered-then-refusing. No automatic fallback between engines. Quotas
(`WORKSPACE_CPU_MINUTES_PER_DAY`, `WORKSPACE_DISK_GB`) meter into `CostEntry`;
idle hibernate after `WORKSPACE_IDLE_SECONDS` (sweep + `manage.py
sweep_workspaces`, beat `workspaces.sweep_workspaces`).

| URL | Method | What | Access | Complexity | Tested | Serializer | DB tables | Notes |
|-----|--------|------|--------|-----------|--------|-----------|-----------|-------|
| `/api/workspaces/hooks/<secret>/` | POST | Job-exit webhook — wakes waiters | **AllowAny** | O(triggers of one user) | `workspaces/tests/test_job_hook.py` | — | Workspace, WorkspaceJob, Trigger | Fires only the **workspace owner's** `Trigger(mode='event', config={event:'job.finished', job_id})` for **this** job, detached via `agents.scheduler.launch` (S4/P2; it used to fire every user's triggers, synchronously, and raised FieldError). An empty secret never matches. **404 for every refusal**. Inbound body is context, never the goal |

---

## 22. Missions — long-horizon goals — app: `missions` — [missions/service.py](../missions/service.py), [missions/sweep.py](../missions/sweep.py)

Models: `Mission` (goal, status, plan, notebook path, budget, deadline,
max_runs, runs_done, wait_for, next_wake_at). `ExecutionLog.mission` FK joins
the chain; `caller='mission'` (in `UNATTENDED_CALLERS`, so it needs
`allow_unattended`). Design note:
[PLATFORM_CAPABILITIES_PLAN.md](PLATFORM_CAPABILITIES_PLAN.md) §10 (P7).

One sweep drives the chain (beat `missions.sweep_missions` + `manage.py
run_missions`): starts runs whose `next_wake_at` is due; event triggers wake
`waiting` ones. `service.after_run` decides done / waiting / next-run /
paused (budget, deadline, max_runs, no-progress ×3). Mission tools
(`mission_status`, `wait_for`, `complete_mission`, `report_progress`,
`start_mission`) exist only in mission runs, except `start_mission` (chat,
`sensitive`, like `create_agent`).

The HTTP routes P7 left out (P10 §18): without these only the model could
start a mission.

| URL | Method | What | Access | Complexity | Tested | Serializer | DB tables | Notes |
|-----|--------|------|--------|-----------|--------|-----------|-----------|-------|
| `/api/missions/` | GET | List the caller's missions | Auth | O(n) | `chat/tests/test_commands.py` | — | Mission | Newest first, capped at 50; carries `open_todos`/`total_todos` so the board renders progress without a second read |
| `/api/missions/` | POST | Start a mission | Auth | O(1) | `chat/tests/test_commands.py` | — (`goal`, `agent_id`, `budget_inr`, `deadline_days`, `max_runs`) | Mission | Through the same service `start_mission` uses: budget required and positive, agent owned, deadline 1–90 days, max runs 1–100. `/goal`'s confirm sheet calls this |
| `/api/missions/<id>/` | GET | One mission | Auth (owner) | O(1) | `chat/tests/test_commands.py` | — | Mission | Foreign ids are 404 (never 403 — the ownership-oracle rule) |
| `/api/missions/<id>/pause/` | POST | Pause a mission | Auth (owner) | O(1) | `chat/tests/test_commands.py` | — | Mission | Chain stops with its notebook intact |
| `/api/missions/<id>/resume/` | POST | Resume a mission | Auth (owner) | O(1) | `chat/tests/test_commands.py` | — | Mission | Re-arms `next_wake_at` to now |
| `/api/missions/<id>/cancel/` | POST | Cancel a mission | Auth (owner) | O(1) | `chat/tests/test_commands.py` | — | Mission | Terminal, like pausing, but final |

Tests: `chat/tests/test_phases_p5_p8.py`, `chat/tests/test_commands.py::MissionRouteTests`.

---

## 23. Dashboards — app: `inference` (`Dashboard`) + tools `render_dashboard` / `save_dashboard`

Model: `Dashboard` (title, spec, sources, refresh_cron, visibility
`link < platform < public`, same 404 rule as published pages).
`render_dashboard` is in `ALWAYS_AVAILABLE` like `render_chart`; tiles are
`kpi | chart | table | text` and a chart tile takes exactly `render_chart`'s
spec. A schedule refreshes sources with no LLM call. Tests:
`chat/tests/test_phases_p5_p8.py::DashboardSpecTests`.

---

## 24. Inbound webhooks and admin sign-in — apps: `messaging`, `esign`, project `urls.py`

| URL | Method | What | Access | Complexity | Tested | Serializer | DB tables | Notes |
|-----|--------|------|--------|-----------|--------|-----------|-----------|-------|
| `/api/messaging/hooks/<channel>/<secret>/` | GET/POST | Provider webhook (Slack, WhatsApp, SMS, Telegram) | **AllowAny** + provider signature | O(1) | `messaging/tests/test_messaging.py` | — | MessagingAccount, InboundMessage, Notification | **404 for every refusal.** Every channel verifies a signature, fail-closed (N4): Slack signing secret, Telegram secret token, WhatsApp `X-Hub-Signature-256` (`WHATSAPP_APP_SECRET`), SMS `X-Twilio-Signature` (`TWILIO_AUTH_TOKEN`, URL from `PUBLIC_URL`, form body). Teams refused until JWT validation exists. GET is WhatsApp's verify handshake only. Body is context, never the goal |
| `/api/esign/hooks/<secret>/` | POST | E-sign provider reports completion | **AllowAny** | O(1) | | — | SignatureRequest, Notification | Path secret is the credential (`secrets.token_urlsafe`); same 404 for every refusal |
| `/admin/login/` | GET/POST | Django admin sign-in | Public | O(1) | `core/tests/test_security_review_round2.py` | — | User | POSTs share the API login's 5/minute throttle (N6); DRF throttles never reached Django's admin view |

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
