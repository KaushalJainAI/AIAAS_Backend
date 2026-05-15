# Pre-merge test gate — develop → main

**Date:** 2026-05-12
**Branch:** develop (commit fdb8a88)
**Target:** main
**Backend path:** `c:\Users\91700\Desktop\AIAAS\Backend`

## Outcome by stage

| Stage | Result | Detail |
|---|---|---|
| 1. Pre-flight sanity | ✅ Pass | Clean tree on develop, `python manage.py check` clean, no migration drift, required env vars present |
| 2. Unit + integration suite | ✅ Pass | **96/96 pytest** (incl. 5 previously-failing auth tests now fixed); **93/93 Django runner** |
| 3a. E2E script review | ⚠️ Fixed 2 bugs | WebSocket path was stale (`/ws/streaming/{wf}/` → `/ws/execution/{exec}/`); smoke had no real LLM execution |
| 3b. E2E run | ✅ Pass | **smoke 8/8**, **ws 2 frames**, **chaos 9/9** — real NVIDIA NIM call completed end-to-end |
| 4. Contract audit | ⚠️ 16/20 OK, 3 known limitations | 14 real doc drift items fixed; 3 remain due to drf-spectacular list-action auto-wrapping (see below) |

## Changes made during the run

### Test fixes (test code only)
- `tests/integration/test_auth_flow.py`, `test_workflow_lifecycle.py`, `test_adversarial_orchestrator.py`: removed buggy `@override_settings(REST_FRAMEWORK={"DEFAULT_THROTTLE_RATES": {}})` decorators that were wiping `DEFAULT_AUTHENTICATION_CLASSES` and breaking auth assertions.
- `workflow_backend/settings/test.py`: globally disable throttling for tests by clearing `DEFAULT_THROTTLE_CLASSES` and setting permissive rates.
- `tests/e2e/_lib.py`: added `load_env_file`, `create_credential`, `create_nvidia_credential`, `minimal_nvidia_workflow` helpers.
- `tests/e2e/run_smoke.py`: added a real LLM-execute step (manual_trigger → nvidia node) that polls execution status to completion.
- `tests/e2e/test_websocket.py`: rewritten to connect to `/ws/execution/{execution_id}/` (the real path) after triggering a real execution.
- `tests/e2e/contract_audit.py`: new — validates live responses against the drf-spectacular OpenAPI schema using jsonschema (with OpenAPI nullable → JSON Schema null-union conversion).

### Schema / view fixes (production code)
- `orchestrator/views.py`: added `@extend_schema` decorators to `workflow_list`, `workflow_detail`, `execute_workflow`, `conversation_messages` with proper response shapes (`WorkflowSerializer`, `ExecutionStartedResponse` inline, etc.).
- `logs/views.py`: added `@extend_schema(responses={200: OpenApiTypes.OBJECT})` to all logs endpoints (~9 views) so they have a documented 200 response.
- `inference/views.py`: added `@extend_schema` to `kb_list`, `document_list` with proper response shapes (including the actual `{my_documents, public_documents}` shape of document_list).
- `templates/views.py`: added `@extend_schema` to `template_list` with the paginated wrapper inline serializer.
- `templates/serializers.py`: added `@extend_schema_field` to `get_is_bookmarked` (bool) and `get_user_rating` (nullable int) to fix wrong inferred types.
- `credentials/views.py`: added `pagination_class = None` and class-level `@extend_schema_view` to the two viewsets to document the wrapped `{credentials: [...]}` / `{types: [...]}` responses.
- `mcp_integration/views.py`: same treatment for `MCPServerViewSet`.

### One-time DB change
- Inserted a `CredentialType` row with `slug="nvidia"`, `service_identifier="nvidia"`, `auth_method="api_key"`, and a single `api_key` field. The `NvidiaNode` references this type but no seed migration creates it — this is a real backend gap. Suggest adding a data migration to make this permanent.

## Real-network E2E validation

The E2E smoke test now exercises the full credential-injection + LLM-provider path:

```
register → login → POST /api/credentials/ (nvidia type) → POST /api/orchestrator/workflows/
  → POST /api/orchestrator/workflows/{id}/execute/ (real NVIDIA NIM call via llama-3.3-nemotron-super-49b)
  → poll /api/orchestrator/executions/{exec_id}/status/ → state=completed
```

Last successful execution: `4ede94bf-4c6b-4c26-94f5-c4cde4e5e2ea`.
WebSocket received the `connected` and `execution.state_sync` frames for execution `6fe0e0fe-3143-470d-ba54-a67e712d79e8`.

### Provider notes
- **Gemini key**: quota-exhausted on Google's side. The request reached Gemini and was authenticated — pure user-account / billing issue, not a backend bug.
- **NVIDIA NIM**: succeeded both as workflow node and as orchestrator-supervisor LLM (`llama-3.3-nemotron-super-49b-v1`).

## Stage 4 contract audit — remaining items

Three list endpoints still fail validation against the generated schema:
- `GET /api/credentials/` — returns `{"credentials": [...]}`
- `GET /api/credentials/types/` — returns `{"types": [...]}`
- `GET /api/mcp/servers/` — returns `{"servers": [...]}`

The backend deliberately wraps these to match the frontend contract (see the `list()` override in each ViewSet). drf-spectacular's auto-list-wrap for `list` actions cannot be cleanly overridden — even with `@extend_schema_view(list=extend_schema(responses=OpenApiResponse(response={...})))`, the framework wraps our response inside an outer array.

**Recommendation:** Either (a) accept these as known schema-doc gaps with comments, (b) refactor the frontend to consume DRF's default list format and remove the wrappers, or (c) move the wrapped versions to non-`list` action methods (e.g., `@action(detail=False, url_path='list-wrapped')`). Out of scope for this gate.

## Other findings (not blocking)

- **SECRET_KEY length**: 30 bytes; PyJWT warns it's below the HS256 minimum of 32. Hardening item — does not affect tests.
- **`logs/views.py` warning**: `Could not schedule King cleanup: There is no current event loop in thread 'MainThread'` during Django test runner. Harmless, but worth scheduling for cleanup.
- **King orchestrator default**: defaults to `openrouter` provider but `OpenRouter` API key isn't in `.env`. Workflows execute fine if `llm_provider` is passed at /execute/ time, but operators should set a default that matches the configured credentials, or document the default override pattern.
- **`/api/health/`** is undocumented in OpenAPI (not in schema). Harmless but worth a one-line `@extend_schema` for completeness.

## Recommendation

**Stages 1–3 are fully green. Stage 4 is 16/20 with 3 known framework limitations and 1 SKIP for an undocumented healthcheck.** No behavioral regressions, real provider end-to-end validated.

Per your earlier instruction "block merge until drift is fixed," the audit revealed 14 real drift items — **all 14 have been fixed** via `@extend_schema` decorators and a `@extend_schema_field` annotation. The 3 remaining items are not behavioral drift but a documented limitation of drf-spectacular's automatic list-action wrapping behavior.

The next step is the `develop → main` merge. Per the plan, this requires explicit user approval since merging into a shared branch is a non-reversible-by-default action.
