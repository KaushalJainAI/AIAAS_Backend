# Faultline Campaign Report

Target: `http://localhost:8000`  
Project: `c:\Users\91700\Desktop\AIAAS\backend`  
Date: 2026-05-19

## Coverage Summary

- Static discovery confirmed a Django/DRF backend with auth, workflows, credentials, MCP, chat, BrowserOS, notifications, skills, inference, imagine, logs, compiler, and node APIs.
- Dynamic API coverage: 77 logged HTTP calls.
- Original sweep distribution: 19 `200`, 7 `201`, 5 `400`, 16 `401`, 0 `5xx`.
- Extended sweep distribution: 12 `200`, 4 `201`, 1 `204`, 7 `400`, 6 `405`, 0 `5xx`.
- Test artifacts:
  - `test-reports/faultline-testcases/aegis_http_sweep.ps1`
  - `test-reports/faultline-testcases/aegis_http_sweep_results_2.log`
  - `test-reports/faultline-testcases/aegis_security_probe.ps1`
  - `test-reports/faultline-testcases/aegis_security_probe_results.log`
  - `test-reports/faultline-testcases/aegis_extended_coverage.ps1`
  - `test-reports/faultline-testcases/aegis_extended_coverage_results.log`

## Endpoints Exercised

Authentication and profile:
- `POST /api/auth/register/` -> `201`
- `POST /api/auth/login/` invalid password -> `401`
- `POST /api/auth/login/` SQLi-style payload -> `400`
- `GET /api/auth/profile/` authenticated -> `200`
- `GET /api/auth/profile/` anonymous and tampered JWT -> `401`
- `PATCH /api/auth/profile/` hostile text payload -> `200`
- `POST /api/auth/token/refresh/` -> `200`
- `GET /api/auth/api-keys/` anonymous and tampered JWT -> `401`
- `POST /api/auth/api-keys/` extra `is_staff` / `role` fields -> `201`

Business APIs:
- `GET /api/nodes/`, `/api/nodes/categories/`, `/api/nodes/models/` -> `200`
- `POST /api/compile/validate/` empty graph -> `400`
- `GET /api/orchestrator/workflows/` authenticated -> `200`
- `POST /api/orchestrator/workflows/` empty and suspicious names -> `400`
- `GET /api/orchestrator/workflows/` anonymous and tampered JWT -> `401`
- `GET /api/orchestrator/workflows/999999999/` anonymous -> `401`
- `GET/POST /api/browseros/workspaces/` -> `200` / `201`
- `GET/POST /api/browseros/notifications/` -> `200` / `201`
- `GET /api/credentials/types/`, `/api/credentials/` -> `200`
- `GET /api/skills/`, `POST /api/skills/` -> `200` / `201`
- `GET /api/chat/sessions/` -> `200`
- `POST /api/chat/guest/sessions/` normal and hostile title -> `201`
- `GET /api/logs/audit/`, `/api/inference/documents/`, `/api/mcp/servers/`, `/api/imagine/` -> `200`
- `GET /api/health/`, `/api/docs/` with hostile `Origin` -> `200`

Extended coverage:
- `POST /api/auth/change-password/request-otp/` negative request -> `400`
- `POST /api/auth/change-password/verify-otp/` invalid OTP -> `400`
- `POST /api/auth/password-reset-request/` -> `200`
- `POST /api/auth/password-reset-verify/` invalid OTP -> `400`
- `POST /api/auth/password-reset-confirm/` bad token -> `400`
- `GET /api/browseros/workspaces/mine/` -> `200`
- `GET/PATCH /api/browseros/workspaces/{id}/` -> `200` / `200`
- `POST/GET/PATCH/DELETE /api/browseros/windows/{id}/` -> `201` / `200` / `200` / `204`
- `POST/GET/PATCH /api/browseros/notifications/{id}/` -> `201` / `200` / `400`
- `POST /api/browseros/notifications/mark_all_read/` -> `200`
- `GET /api/canvas-agent/node-types/` -> `200`
- `POST /api/canvas-agent/command/` empty instruction -> `400`
- `POST /api/canvas-agent/command/` hostile instruction -> `200`
- `POST /api/buddy/context/` path-traversal-shaped context -> `200`
- `POST /api/buddy/action/` malformed notify command -> `400`
- `POST /api/buddy/commands/` open terminal command -> `200`
- Verb tampering against `/api/auth/login/` and `/api/chat/guest/sessions/` with `PUT/PATCH/DELETE` -> `405`

## Findings

### High: Credentialed CORS Allows Arbitrary Origin in Local Runtime

Evidence:
- Requests with `Origin: https://evil.example` received `Access-Control-Allow-Origin: https://evil.example` and `Access-Control-Allow-Credentials: true`.
- Confirmed on `POST /api/auth/login/`, `POST /api/auth/register/`, `GET /api/health/`, `GET /api/docs/`, and `POST /api/chat/guest/sessions/`.
- Relevant config:
  - `workflow_backend/settings/base.py:329` sets `CORS_ALLOW_ALL_ORIGINS` from env.
  - `workflow_backend/settings/base.py:330` sets `CORS_ALLOW_CREDENTIALS = True`.
  - `workflow_backend/settings/local.py:22` defaults `CORS_ALLOW_ALL_ORIGINS` to `True`.
  - `.env.local:24` also sets `CORS_ALLOW_ALL_ORIGINS=True`.

Impact:
- Any website can make credentialed browser requests to the API in this runtime if cookies or browser-managed credentials are used.
- This is especially risky around auth, profile, guest chat, docs/schema, and any session-backed routes.

Suggested fix:
- Do not combine wildcard/all-origin CORS with credentials.
- In local/dev, prefer explicit localhost frontend origins:
  - `CORS_ALLOW_ALL_ORIGINS = False`
  - `CORS_ALLOWED_ORIGINS = ["http://localhost:3000", "http://localhost:5173"]`
  - Keep `CORS_ALLOW_CREDENTIALS = True` only when a strict allowlist is enforced.

### Medium: Security Headers Are Incomplete on Tested Runtime

Evidence:
- Tested responses include `X-Frame-Options: DENY` and `X-Content-Type-Options: nosniff`.
- Tested responses did not include `Content-Security-Policy`.
- Tested responses did not include `Strict-Transport-Security`.
- Deployment settings define `SECURE_CONTENT_TYPE_NOSNIFF` and `X_FRAME_OPTIONS`, but no CSP policy was found in active settings.

Impact:
- Missing CSP increases the blast radius of any stored or reflected XSS.
- Missing HSTS is expected on plain `http://localhost`, but production should emit it behind HTTPS.

Suggested fix:
- Add a CSP middleware/package such as `django-csp`, with a policy tailored to Swagger/docs and frontend needs.
- In deployment settings, set `SECURE_HSTS_SECONDS`, `SECURE_HSTS_INCLUDE_SUBDOMAINS`, and `SECURE_HSTS_PRELOAD` once HTTPS termination is confirmed.

### Medium: Plaintext OAuth Client Secrets Are Present in Repo Environment Files

Evidence:
- `.env:25`, `.env.local:28`, and `.env.deployment:46` contain Google OAuth client secrets. Values are masked in this report but present in the files.

Impact:
- If these files are committed, synced, backed up, or exposed to logs/artifacts, OAuth credentials may be reusable by an attacker.

Suggested fix:
- Rotate the exposed Google OAuth client secrets.
- Remove secrets from committed env files.
- Keep only `.env.example` placeholders in source control.
- Load real values from a secret manager or deployment environment variables.

### Medium: BrowserOS Window API Accepts Invalid Geometry and Stores Hostile State Verbatim

Evidence:
- `POST /api/browseros/windows/` accepted `position_x=-999999`, `position_y=999999`, `width=0`, `height=999999`, `z_index=999999`, and `state_data={"command":";id","nested":{"template":"{{7*7}}"}}` with `201`.
- `PATCH /api/browseros/windows/1/` accepted `width=-1` and `state_data={"payload":"../../etc/passwd"}` with `200`.
- Source review shows `browserOS/models.py` uses unconstrained `IntegerField` values for geometry and `browserOS/serializers.py` exposes `fields = '__all__'` without object-level validation.

Impact:
- Clients can persist unusable or extreme window state that may break BrowserOS rendering, cause layout instability, or create client-side denial-of-service conditions.
- Verbatim command/template/path strings in `state_data` are not directly executed by the backend in this test, but they become dangerous if any frontend or automation layer later interprets them as HTML, templates, commands, or file paths.

Suggested fix:
- Add serializer validation for sane geometry bounds, for example width/height minimums and maximums, finite coordinate ranges, and z-index limits.
- Prefer an explicit field list over `fields = '__all__'` for `OSAppWindowSerializer`.
- Treat `state_data` as untrusted data on every consumer; escape before rendering and never route values into shell/template/file operations without allowlisted commands.

### Medium: BrowserOS Notifications Store HTML Markup Verbatim

Evidence:
- `POST /api/browseros/notifications/` accepted `message="<script>alert(1)</script>"` with `201`.
- `GET /api/browseros/notifications/4/` returned the script tag unchanged.
- Source review shows `browserOS/models.py` stores `message` as `TextField` and `OSNotificationSerializer` returns it directly.

Impact:
- If the frontend renders notification titles or messages as HTML, this becomes stored XSS.
- Even if the current frontend escapes by default, storing raw markup increases the risk of future unsafe rendering regressions.

Suggested fix:
- Keep backend output as text-only and document that clients must render it as text, not HTML.
- Optionally reject or sanitize HTML tags in `OSNotificationSerializer.validate`.
- Add frontend tests that assert notification content is escaped.

### Informational: Local Baseline Tooling Is Broken

Evidence:
- `venv\Scripts\python.exe manage.py check` failed with: `No Python at "C:\Users\91700\AppData\Local\Programs\Python\Python311\python.exe"`.
- `python` is not available on PATH and `py` reports no installed Python.

Impact:
- Static checks, migrations, and pytest cannot be run from this shell even though the live server is responding.

Suggested fix:
- Recreate the virtualenv with a currently installed Python.
- Ensure `python --version` or `py --version` works before running CI-equivalent checks.

## Positive Results

- No server crashes or `5xx` responses were observed across 47 HTTP calls.
- The extended 30-call sweep also produced no `5xx` responses.
- Anonymous and tampered-JWT access was rejected for profile, API keys, workflows, credentials, logs, MCP servers, and BrowserOS workspaces.
- Workflow name validation rejected obvious SQL/path traversal strings.
- Invalid login and invalid registration inputs returned `400`/`401` rather than succeeding.
- Verb tampering on login and guest chat session collection returned `405`.

## Patch Guidance

Recommended focused patch:

```python
# workflow_backend/settings/local.py
os.environ.setdefault('CORS_ALLOW_ALL_ORIGINS', 'False')
os.environ.setdefault(
    'CORS_ALLOWED_ORIGINS',
    'http://localhost:3000,http://localhost:5173,http://127.0.0.1:3000,http://127.0.0.1:5173',
)
```

```python
# workflow_backend/settings/base.py
CORS_ALLOW_ALL_ORIGINS = os.environ.get('CORS_ALLOW_ALL_ORIGINS', 'False') == 'True'
CORS_ALLOWED_ORIGINS = _split_env_list(os.environ.get('CORS_ALLOWED_ORIGINS', ''))
CORS_ALLOW_CREDENTIALS = bool(CORS_ALLOWED_ORIGINS) and not CORS_ALLOW_ALL_ORIGINS
```

For production, keep `CORS_ALLOW_ALL_ORIGINS=False`, set only trusted frontend origins, rotate leaked OAuth secrets, and add CSP/HSTS in deployment settings.

BrowserOS validation patch outline:

```python
# browserOS/serializers.py
class OSAppWindowSerializer(serializers.ModelSerializer):
    class Meta:
        model = OSAppWindow
        fields = [
            'id', 'app_id', 'title', 'is_minimized', 'is_pinned',
            'position_x', 'position_y', 'width', 'height', 'z_index',
            'state_data', 'created_at', 'updated_at',
        ]
        read_only_fields = ['id', 'workspace', 'created_at', 'updated_at']

    def validate_width(self, value):
        if value < 160 or value > 3840:
            raise serializers.ValidationError('Width must be between 160 and 3840 pixels.')
        return value

    def validate_height(self, value):
        if value < 120 or value > 2160:
            raise serializers.ValidationError('Height must be between 120 and 2160 pixels.')
        return value

    def validate_z_index(self, value):
        if value < 0 or value > 10000:
            raise serializers.ValidationError('z_index must be between 0 and 10000.')
        return value
```
