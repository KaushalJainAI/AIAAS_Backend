# `credentials/`: saved API keys and logins

Stores the keys and OAuth tokens a user connects (OpenAI key, Google login,
Slack token...), **encrypted** with `CREDENTIAL_ENCRYPTION_KEY`.

Design: [`docs/CREDENTIALS_AND_SECURITY.md`](../docs/CREDENTIALS_AND_SECURITY.md).

## Data (`models.py`)

| Model | What it is |
|---|---|
| `CredentialType` | A kind of credential and its fields ("OpenAI: api_key"). Created by a migration, so a fresh install has them |
| `Credential` | One saved credential for one user. Secret fields are encrypted |
| `CredentialAuditLog` | Who used or changed a credential, and when |

## Files

| File | What it does |
|---|---|
| `manager.py` | `CredentialManager`: **the one way to read a credential**. Decrypts, caches for 5 minutes, refreshes OAuth tokens |
| `resolution.py` | "Which key do we use to call provider X for user Y?" |
| `refs.py` | Secret references: a tool argument can *name* a credential (`{"secret_ref": "slug.field"}`) instead of containing it |
| `oauth.py` | `GoogleOAuthProvider`: the Google sign-in flow for connecting an account |
| `verification.py` | `CredentialVerifier`: testing a credential actually works |
| `views.py`, `urls.py`, `serializers.py` | `/api/credentials/`. Secrets are never sent back to the browser |
| `browser_utils.py` | Logging into a site with a headless browser and collecting its auth tokens |

## Watch out for

- `CredentialManager` keeps a **process-wide cache**. Tests that read
  credentials must clear it in `setUp`, because test databases reuse ids.
- `CredentialType` rows already exist in tests (from migrations). Use
  `update_or_create`, not `create`.

## Management commands

`seed_connector_credentials`: the credential types connectors need (the
migration calls the same code).
