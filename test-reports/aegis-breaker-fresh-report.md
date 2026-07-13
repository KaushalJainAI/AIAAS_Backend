# Aegis-Breaker Fresh Campaign Report

Target: `http://localhost:8000`  
Project: `C:\Users\91700\Desktop\AIAAS\backend`  
Date: 2026-05-20

## Coverage

Fresh dynamic probe: `testcases/aegis_breaker_fresh_probe.ps1`

Result artifact: `testcases/aegis_breaker_fresh_results.json`

The fresh probe made 12 logged HTTP calls:

| Status | Count |
| --- | ---: |
| 200 | 4 |
| 201 | 4 |
| 400 | 2 |
| 401 | 2 |

Endpoints exercised:

- `GET /api/health/` with hostile `Origin`
- `GET /api/auth/profile/` anonymous and authenticated
- `GET /api/auth/api-keys/` anonymous
- `POST /api/auth/register/`
- `POST /api/auth/login/` with SQLi-shaped credentials
- `POST /api/auth/token/refresh/`
- `POST /api/auth/api-keys/` with mass-assignment-shaped fields
- `POST /api/browseros/windows/` with invalid geometry and hostile JSON state
- `POST /api/browseros/notifications/` with HTML/script markup
- `POST /api/compile/validate/` with empty graph and template payload
- `GET /api/orchestrator/workflows/`

Baseline tooling note: `venv\Scripts\python.exe manage.py check` failed before Django startup because the virtualenv references a missing interpreter at `C:\Users\91700\AppData\Local\Programs\Python\Python311\python.exe`.

## Findings

### High: Credentialed CORS Reflects Arbitrary Origin

Evidence:

- Fresh request: `GET /api/health/` with `Origin: https://evil.example`.
- Response: `200` with `Access-Control-Allow-Origin: https://evil.example` and `Access-Control-Allow-Credentials: true`.
- Source: `workflow_backend/settings/base.py:329` reads `CORS_ALLOW_ALL_ORIGINS`; `workflow_backend/settings/base.py:330` sets `CORS_ALLOW_CREDENTIALS = True`.
- Source: `workflow_backend/settings/local.py:22` defaults `CORS_ALLOW_ALL_ORIGINS` to `True`.

Impact:

Any website can make credentialed browser requests to the API in this runtime if browser-managed credentials are present. This is high-risk around auth/profile, API key, workflow, and credential APIs.

Fix:

Set `CORS_ALLOW_ALL_ORIGINS = False`, configure a strict `CORS_ALLOWED_ORIGINS` allowlist for known frontend origins, and only enable `CORS_ALLOW_CREDENTIALS` when the allowlist is non-empty and all-origin CORS is disabled.

### High: Secrets Are Stored in Repo Environment Files

Evidence:

- `.env`, `.env.local`, and `.env.deployment` contain live-looking API keys, OAuth client secrets, bot tokens, database passwords, and Django secret keys.
- Examples include Google OAuth client secrets, Gemini/NVIDIA/Tavily/Perplexity keys, Telegram bot token, `POSTGRES_PASSWORD`, and `SECRET_KEY` values.

Impact:

If these files are committed, synced, shared, or included in artifacts, attackers can reuse third-party service tokens, impersonate integrations, access databases, or forge Django-signed data depending on deployment exposure.

Fix:

Rotate every exposed credential, remove real secrets from repo-tracked env files, keep only placeholder `.env.example` values, and load production secrets from deployment environment variables or a secret manager.

### Medium: BrowserOS Window API Accepts Invalid Geometry and Hostile State

Evidence:

- Fresh request: `POST /api/browseros/windows/` accepted `position_x=-999999`, `position_y=999999`, `width=0`, `height=-1`, `z_index=999999`, and `state_data` containing `;id`, `{{7*7}}`, and `../../etc/passwd`.
- Response: `201`, with the invalid geometry and hostile state echoed back.
- Source: `browserOS/models.py:29-33` uses unconstrained `IntegerField` values for geometry.
- Source: `browserOS/models.py:36` stores arbitrary `state_data` JSON.
- Source: `browserOS/serializers.py:7` exposes `fields = '__all__'` without validation.

Impact:

Malformed persisted window state can break or destabilize the BrowserOS UI. Hostile strings are not executed by this backend probe, but storing them verbatim increases downstream risk if frontend, automation, template, shell, or file consumers later interpret the values.

Fix:

Add serializer validation for sane bounds: positive width/height, maximum dimensions, finite coordinate ranges, and reasonable z-index limits. Use an explicit serializer field list and treat `state_data` as untrusted input at every consumer.

### Medium: BrowserOS Notifications Store Script Markup Verbatim

Evidence:

- Fresh request: `POST /api/browseros/notifications/` accepted `message="<script>alert(1)</script>"`.
- Response: `201`, with the script markup returned unchanged.
- Source: `browserOS/models.py:56` stores notification `message` as plain `TextField`.
- Source: `browserOS/serializers.py:21` returns `message` directly.

Impact:

If any client renders notification content as HTML, this becomes stored XSS. Even when current React-style rendering escapes by default, raw markup in persistent notification content is a regression trap.

Fix:

Render notification content as text only on clients, add frontend tests for escaping, and consider backend validation or sanitization that rejects HTML tags for notification `title` and `message`.

### Low: Local Baseline Tooling Is Broken

Evidence:

- `venv\Scripts\python.exe manage.py check` failed with: `No Python at '"C:\Users\91700\AppData\Local\Programs\Python\Python311\python.exe'`.

Impact:

Developers and CI-equivalent local campaigns cannot run Django checks or pytest from this workspace until the virtualenv is repaired.

Fix:

Recreate the virtualenv with an installed Python interpreter and verify `python --version`, `manage.py check`, and pytest collection before relying on local test results.

## Positive Results

- No `5xx` responses were observed in the fresh 12-call probe.
- Anonymous access to `/api/auth/profile/` and `/api/auth/api-keys/` returned `401`.
- SQLi-shaped login credentials returned `400`, not a successful login.
- Authenticated profile retrieval, token refresh, and workflow listing worked.
- Empty compiler validation returned `400`, not a crash.
