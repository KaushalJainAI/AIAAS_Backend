# Credentials System Architecture

The credentials system provides a secure mechanism for managing and using sensitive authentication data (API keys, passwords, tokens) within workflows.

## Core Concepts

### 1. Credential Types (`CredentialType`)
Defines the schema and metadata for a specific integration.
- **Purpose**: Acts as a template for what information is required.
- **Key Fields**:
    - `name`: Display name (e.g., "OpenAI", "PostgreSQL").
    - `auth_method`: implementations like `api_key`, `oauth2`, `basic`, `bearer`.
    - `fields_schema`: A JSON definition of the form fields required (e.g., "Host", "User", "Password").
    - `oauth_config`: Configuration for OAuth flows (auth URL, token URL).

### 2. User Credentials (`Credential`)
Stores the actual sensitive data owned by a user.
- **Security**: Data is **encrypted at rest** using Fernet symmetric encryption (`cryptography` library).
- **Ownership**: Strictly scoped to the creating `User`.
- **Storage**: The `encrypted_data` field holds the binary blob of the JSON payload.
- **Key Access**: Encryption uses a system-wide `CREDENTIAL_ENCRYPTION_KEY` located in Django settings.

### 3. Usage Lifecycle

#### Creation
1. User selects a `CredentialType` (e.g., "Google Sheets").
2. Frontend renders form based on `fields_schema`.
3. User submits data.
4. Backend encrypts the payload immediately and saves it as a `Credential`.

#### Compilation
1. User adds a Node to a workflow that requires credentials.
2. The `config` stores the `credential_id` reference.
3. **Validation**: The `Compiler` verifies that `credential.user_id` matches the executing user.

#### Execution
1. **Load**: `executor.services.load_credentials_for_workflow` fetches referenced credentials.
2. **Decrypt**: The system calls `.decrypt_data()` to get the raw secrets.
3. **Inject**: Decrypted secrets are placed into the `ExecutionContext` (in-memory only).
4. **Run**: The `NodeHandler` accesses secrets via `context.credentials` to authenticate external requests.
5. **Cleanup**: Secrets exist in memory only for the duration of the execution.

## Data Model

```python
class Credential(models.Model):
    user = ForeignKey(User)
    credential_type = ForeignKey(CredentialType)
    encrypted_data = BinaryField() # The secure payload
    
    # OAuth specific fields
    access_token = BinaryField()
    refresh_token = BinaryField()
```

## Security Best Practices
- **Encryption Key**: The `CREDENTIAL_ENCRYPTION_KEY` MUST be kept secure (e.g., Environment Variable, Vault) and never committed to source control.
- **Scope**: Credentials should only be accessible to the workflow execution engine and the owning user.
- **Logs**: The execution logs must NEVER record raw credential values. Input/Output data scrubbing should be implemented if nodes output sensitive data.
