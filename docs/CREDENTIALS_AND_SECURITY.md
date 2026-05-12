# Credentials & Security Architecture

AIAAS is designed with a "Security-First" approach to handle sensitive API keys, OAuth tokens, and user data.

## 1. Encryption at Rest

All sensitive credential data is encrypted at the database level using **AES-256 Symmetric Encryption** via the `cryptography.fernet` library.

- **Symmetric Key**: The system uses a `CREDENTIAL_ENCRYPTION_KEY` (stored in `.env`, never in source control).
- **Encrypted Fields**:
    - `encrypted_data`: Main JSON payload for API keys and secrets.
    - `access_token`: OAuth2 access tokens.
    - `refresh_token`: OAuth2 refresh tokens.
- **Storage**: Credentials are stored in the `Credential` model's `BinaryField` to prevent character encoding issues during storage and retrieval.

## 2. Access Control & Ownership

The system enforces strict multi-tenancy at every layer:

### Database Level
- The `Credential` model has a mandatory `ForeignKey` to the `User`. 
- Queries always filter by `user=request.user` to prevent cross-account access.

### Compiler Level (`validate_credentials`)
- During workflow compilation, the `WorkflowCompiler` scans every node for credential IDs.
- It cross-references these IDs against the set of **active** credentials owned by the authenticated user.
- If a workflow tries to use a credential ID that belongs to another user (or doesn't exist), the compiler raises a `missing_credential` error and halts execution.

## 3. OAuth2 Management

The platform includes a robust OAuth2 lifecycle manager:
- **Auto-Refresh**: The `get_valid_access_token()` method automatically detects expired tokens and uses the `refresh_token` to fetch a new one from the provider (e.g., Google, Slack) before the node executes.
- **Atomic Refresh**: Uses database locks (`select_for_update`) to prevent race conditions when multiple concurrent workflows try to refresh the same credential.

## 4. Audit Logging

Every interaction with a sensitive credential is recorded in the `CredentialAuditLog`:
- **Accessed**: When a credential is decrypted for a workflow run.
- **Updated**: When a user changes the secret or a token is auto-refreshed.
- **Context**: The log captures the `user_id`, `timestamp`, `ip_address`, and even the `workflow_id` that triggered the access.

## 5. Network Security

- **EC2 Firewall**: The production PostgreSQL database is protected by AWS Security Groups, restricted to the pgbouncer port (6432) and direct admin access (5432) only from authorized IPs.
- **JWT Authentication**: All API endpoints require a valid JSON Web Token.

---

**Source Reference**: [credentials/models.py](file:///c:/Users/91700/Desktop/AIAAS/Backend/credentials/models.py)
