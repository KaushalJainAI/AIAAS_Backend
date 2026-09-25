# Security Review Fix Plan (2026-09-25)

A whole-project review (not a diff review) of the backend, the web frontend,
and the deploy config (Caddy, nginx, `docker-compose.prod.yml`). BrowserOS was
skipped (parked). This file is the plan and the record of what was done.
Lesson write-up: `learning/15_whole_project_security_review.md`.

Status: **implemented 2026-09-25** (items S1–S9, S11, P1–P3, and round two N1–N8). S10 is a machine
housekeeping step for the user. Not deployed.

---

## Security findings and fixes

| # | Severity | Problem | Fix |
|---|---|---|---|
| S1 | High | **Pre-account takeover.** Signup never verifies email; Google sign-in logged into any existing account with that email, and did not check Google's `email_verified`. An attacker registers `victim@gmail.com`, the victim later signs in with Google into the attacker's account, the attacker keeps the password. | Google sign-in refuses an unverified Google email. When it links to an existing account whose email was never proven (`UserProfile.email_verified_at IS NULL`) that still has a password, the password is made unusable and every old token is revoked, so whoever set it is locked out. Email is marked verified by Google login, password reset, password change and email change (each reached the inbox). Lookup is case-insensitive, matching signup. |
| S2 | Medium | **Password change/reset did not end other sessions.** Access tokens live 24 h, refresh 30 d; a stolen token survived a reset. | `UserProfile.tokens_valid_after` + `core/auth/revocation.py`. Every JWT door (REST auth class, refresh endpoint, WebSocket middleware) refuses a token whose `iat` is older. Reset, change, email change and S1 linking all revoke. Change-password and email-change return a fresh token pair so the current tab stays signed in (frontend stores it: `api/auth.ts::keepFreshPair`). |
| S3 | Medium | **`?token=` accepted on every GET.** A global default auth class, so a token in any URL authenticated — and URLs land in logs, history and `Referer`. The web app never used it for HTTP. | Removed from `DEFAULT_AUTHENTICATION_CLASSES` and the class deleted. The WebSocket keeps its own `?token=` (browsers cannot set WS headers). |
| S4 | Medium (dormant) | **Workspace job webhook fired every user's `job.finished` triggers**, not just the workspace owner's, and ran each agent to completion inside the HTTP request. Dormant only because nothing writes `Workspace.secret`. | Triggers filtered to the workspace owner and to a matching `job_id` (an empty filter no longer matches every job). Runs start detached through `agents.scheduler.launch`, the scheduler's own path. An empty secret can never match. While fixing it: the old query filtered on `event`/`filter` columns that do not exist (the event lives in `Trigger.config`), so it raised `FieldError` on every call, hidden by `except Exception: pass` — the feature had never worked. |
| S5 | Low–Medium | **`CORS_ALLOW_ALL_ORIGINS=True` in production** with credentials on, overriding the allow-list one line above. Harmless only while no API route reads a cookie. | Line removed from `docker-compose.prod.yml`; the explicit `CORS_ALLOWED_ORIGINS` now governs. |
| S6 | Low | **API keys stored in plain text**, and the list endpoint returned the full key on every read (the serializer said "shown once" and wasn't). | Keys stored as SHA-256 (`key` column holds the hash; migration `core.0012` hashes existing rows, which keep working). Plaintext returned only on create/rotate. List returns `key_prefix`, never `key`; Settings shows the masked prefix and only offers Copy for a freshly generated key. |
| S7 | Low | **OTP from `random.randint`** (Mersenne Twister, not a CSPRNG). | `secrets.randbelow`. |
| S8 | Low | **PDF previews in unsandboxed `blob:` frames**, and file downloads with no CSP. Safe today only because the served type follows the `.pdf` extension. | Frontend re-types the blob to `application/pdf` before framing it (`useBlobUrl(id, asType)`, `AuthenticatedMediaPreview`), so an HTML body can never render there. Backend adds `Content-Security-Policy: sandbox …` and `nosniff` to every document, version and published-page file response (`inference/utils.harden_file_response`). |
| S9 | Low | **Link cards `window.open` any model-supplied URL.** | `lib/safeUrl.ts::openExternal` — only `http:`/`https:` open. |
| S11 | Medium | **Refresh tokens were reusable.** `BLACKLIST_AFTER_ROTATION=True` with the comment "a stolen refresh token is single-use", but `rest_framework_simplejwt.token_blacklist` was not installed, so simplejwt swallowed the `AttributeError` and a used refresh token kept working for 30 days. Found while fixing S2. | App installed (its migrations add the blacklist tables). Pinned by a test that uses a refresh token twice. Expired rows: `manage.py flushexpiredtokens`, worth adding to a beat schedule. |
| S10 | Housekeeping | `my-pem.pem` (SSH key) sits world-readable in the project root. | User action: move to `~/.ssh/`, restrict permissions. Not moved automatically — scripts may name the path. |

Checked and fine: `SECRET_KEY` required outside DEBUG; login/register/reset
throttled and `X-Forwarded-For` not spoofable (Caddy replaces it); agent
webhook opaque 404s; WebSocket consumers check ownership; every HTML iframe
lacks `allow-same-origin`; stdio MCP off in prod; sandbox is the sidecar.

---

## Round two (same day): N1–N8

A second pass over outbound requests, public webhooks, OAuth, file parsing and
the data sources.

| # | Severity | Problem | Fix |
|---|---|---|---|
| N1 | High | **`download_file` followed redirects unchecked** (`requests.get`). A public URL that 302s to `169.254.169.254` (EC2 metadata, which can hold instance credentials) or an internal service was fetched and saved into the user's files. A web page the agent reads can steer it there. | `core/safety/net.py::fetch_file`: urllib with the redirect handler that re-runs the SSRF guard on every hop; an oversized body raises `FetchTooLarge` instead of truncating. |
| N2 | Medium | **Zip bombs.** `.docx`/`.xlsx`/`.pptx` are zips, read whole with no size check; a 1 MB file can inflate to ~1 GB on a 913 MB box. Any free account could upload one. | `inference/utils.py::zip_within_budget` sums the declared uncompressed sizes before anything is opened (`ZIP_UNCOMPRESSED_LIMIT`, 200 MB). Checked at the doors — upload validation, `vfs.write_binary` (downloads, API responses), chat attachments — and in every extractor. |
| N3 | Medium | **MCP OAuth discovery followed redirects** past `_guard`, echoing 200 chars of the response in its error. | `follow_redirects=False`; a 3xx counts as "not published". |
| N4 | Medium | **WhatsApp, SMS and Teams webhooks verified nothing** but the path secret, while the docstring claimed signatures. Forged inbound messages reach agents as context. | WhatsApp: `X-Hub-Signature-256` with `WHATSAPP_APP_SECRET`. SMS: `X-Twilio-Signature` with `TWILIO_AUTH_TOKEN`, over the URL rebuilt from `PUBLIC_URL` (and the body is now parsed as Twilio's form, not JSON — SMS inbound never parsed before). Teams: refused until Bot Framework JWT validation exists. All fail closed when the secret is unset. |
| N5 | Medium (bug) | **nginx's 1 MB default body limit** refused uploads over 1 MB with a 413, while the backend allows 50 MB — and it was the only thing capping Django's 100 MB in-memory body limit on public routes. | `client_max_body_size 55m` on `/api/`; `DATA_UPLOAD_MAX_MEMORY_SIZE` 100 → 20 MB; `FILE_UPLOAD_MAX_MEMORY_SIZE` 100 → 10 MB (larger files spool to disk). |
| N6 | Low–Medium | **Django admin login had no brute-force limit** (DRF throttles do not reach it). | `/admin/login/` is wrapped with the API login's 5/minute throttle (`workflow_backend/urls.py::admin_login`). |
| N7 | Low | **SQLite writes allowed `ATTACH`** (open/create any file) and `PRAGMA` (sqlparse tokenises it as a name, so the keyword scan missed it). | `ATTACH`, `DETACH`, `PRAGMA` refused, the last by its leading token. |
| N8 | Low | **Generated media fetched from the provider's URL** with redirects followed and no private-address check. | Same `fetch_file` as N1 (`chat/tools/media.py`, `imagine/services/documents.py`). |

Tests: `core/tests/test_security_review_round2.py` (N1–N3, N6–N8, with a local
server that redirects to the metadata address), `messaging/tests/test_messaging.py`
(N4: signed/unsigned WhatsApp, signed/forged Twilio, Teams refused).

Deploy: set `WHATSAPP_APP_SECRET` / `TWILIO_AUTH_TOKEN` before using those
channels — without them their webhooks now refuse everything.

---

## Performance improvements found in the same pass

| # | Problem | Fix |
|---|---|---|
| P1 | API-key auth wrote `last_used_at` on **every** request — a DB write lock per call (sharpest on SQLite). | Written at most once per `TOUCH_SECONDS` (60 s), as a single `UPDATE`. |
| P2 | Workspace webhook ran agents **synchronously** in the request (same as S4). | Detached start, shared with the scheduler. |
| P3 | Google sign-up picked a username with one `exists()` query per collision. | One query for all taken `base*` names (`_free_username`). |

### How the larger performance work is being handled

The "lags with several runs" problem is not re-planned here; it has its own
plan, `Backend/docs/CONCURRENCY_LAG_FIX_PLAN.md`, whose Phases 0–5 are built:
DB connections released across model calls, the SQLite checkpointer's lock and
full-state serialise taken off the event loop, a wider `sync_to_async` pool,
and memory limits. Remaining: **Phase 5 needs the EC2 resize** (t3.small) to
take effect, and **Phase 6 is conditional on Phase 0's measurements** from
production (`[Latency]` lines). The order is: deploy these fixes → resize →
measure under load → decide Phase 6. P1–P3 above are small, local wins that do
not depend on that sequence.

---

## Tests

- `core/tests/test_security_review.py` — S1, S2, S3, S6, S7, S11, P1, P3.
- `workspaces/tests/test_job_hook.py` — S4/P2.
- `src/lib/__tests__/safeUrl.test.ts` — S9.

Existing suites re-run green after the change: `core`, `workspaces`,
`streaming`, `agents` triggers/schedules/scheduler/regressions, `inference`
published pages/regressions/filesystem, `chat/tests/test_phases_p5_p8.py`
(about 440 tests); `makemigrations --check` clean; frontend `tsc -b --force`
and eslint clean on the touched files.

## Deploy notes

- Run migrations: `core.0012` (profile fields + hashes existing API keys in
  place) and the `token_blacklist` app's own.
- **Everyone stays signed in** — the migration sets no cutoff.
- A user who registered with a password and later signs in with Google for the
  first time loses that password (the S1 tradeoff); "Forgot password" restores it.
- BrowserOS was not checked (its source is not in this checkout). If its build
  sends `?token=` on any HTTP GET, those calls now 401 (S3).
