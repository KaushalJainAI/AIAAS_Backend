# MCP Integration Architecture

The **Model Context Protocol (MCP)** is the backbone of the AIAAS tool system. It allows the platform to connect to local or remote servers that provide specialized tools (like Browser Automation, SQL query execution, or Google Search) and expose them to the AI.

## 1. Technical Components

### A. MCP Client Manager (`client.py`)
This is the low-level transport layer.
- **Connection Pooling**: To avoid the 500ms overhead of spawning subprocesses for every tool call, the manager pools `ClientSession` objects per `(server_id, user_id)` for 5 minutes.
- **Transports**: Supports both **Stdio** (local subprocesses) and **SSE** (remote HTTP streams).
- **Concurrency**: MCP sessions are not thread-safe; the manager uses `asyncio.Lock` to serialize access to shared sessions.
- **Visibility re-check**: the manager re-validates server visibility, enabled state, and the user's preference at connection time, so a deleted server fails closed.

### B. Credential Injector (`credential_injector.py`)
This is the "Security Bridge" between the user's encrypted vault and the MCP server.
- **Environment Mapping**: Injects credentials directly into the environment variables of a `stdio` process.
- **Header Mapping**: Injects credentials into the HTTP headers of an `SSE` stream, with `{slug:field}` template placeholders (e.g. `"Bearer {github_token:api_key}"`).
- **Resolution**: Decrypts vault values just-in-time for the connection, immediately before transport open. Never logged, never returned to the frontend.
- **Fallback**: Field lookup falls back to a Credential's `access_token`/`refresh_token` columns, where the OAuth flow stores tokens.

### C. Tool Provider & Cache (`tool_provider.py` / `tool_cache.py`)
- **Runtime minting**: Tools are not declared anywhere — they are minted at runtime from each server's advertised catalogue and namespaced `mcp__<server_id>__<tool>_<digest>` so they never collide with built-ins.
- **Gating**: A server with missing/invalid credentials is skipped at advertisement time, so a tool the user cannot call is never offered. Per-server 5s timeout; servers are queried concurrently so one hung process cannot stall an agent turn.
- **Aggregation**: Combines tools from all enabled servers (User-owned + System-wide, minus any the user disabled via `MCPServerPreference`) into the chat agent's and agent runs' tool loops.
- **Caching**: Tool definitions (names, descriptions, schemas) are cached in Redis (120 s TTL) to prevent excessive "ListTools" calls.

### D. Models (`models.py`)
- `MCPServer` — one server: transport config, credential wiring maps, presentation metadata (`icon_slug`, `category`, `tagline`). `user=NULL` rows are shared read-only templates.
- `MCPServerPreference` — per-user enable/disable state; a missing row means "inherit the server's flag". The API exposes `effective_enabled` so the UI renders one truth.

## 2. Integration Flow

1.  **User Configures Server**: A user adds an MCP server (e.g., `npx -y @modelcontextprotocol/server-postgres`) and defines which credential types it needs. `command` must be an allowed launcher (`ALLOWED_STDIO_COMMANDS` — a stdio subprocess with user credentials in its env is RCE if unrestricted), and SSE URLs are SSRF-checked at registration and again at every connection (DNS rebinding).
2.  **Credential Readiness**: checked in two places — at advertisement (missing credential ⇒ server skipped) and at execution (missing/invalid credential ⇒ structured `credential_missing` / `credential_invalid` error JSON, not an exception). The DAG-era `workflow_validator.py` was deleted with the runtime it guarded.
3.  **Tool Execution**:
    - The LLM requests a tool call (`mcp__<id>__<tool>_<digest>`).
    - The `MCPClientManager` fetches/creates a pooled session.
    - The `CredentialInjector` decrypts the user's secrets and applies them to the session transport.
    - The tool is executed, and the result is serialized into JSON for the AI.
    - Credentialed calls are gated by a read-only verb allowlist in `chat/tools/permissions.py` (chat); agent runs use the stricter `unattended_policy`.

## 3. Security Hardening

- **User Scoping**: MCP servers can be marked as "User Owned," meaning only that user's process can see or call those tools; `user=NULL` rows are shared templates, read-only over the API (except per-user enable/disable).
- **Zero-Exposure Policy**: Credentials are only decrypted inside the `CredentialInjector` during the handshake phase. They are never returned to the frontend or exposed in logs.
- **Subprocess Isolation**: `stdio` servers run as independent subprocesses, ensuring that if a server crashes, it doesn't take down the Django backend.
- **Launcher Allowlist**: the API only accepts known MCP launchers (`npx`, `node`, `uv`, `docker`, ...) as `command`; anything else 400s.
- **SSRF Guard**: user-supplied SSE URLs must not resolve to private/link-local addresses — validated at registration and re-validated per connection.

---

**Source Reference**: [mcp_integration/client.py](file:///c:/Users/91700/Desktop/AIAAS/Backend/mcp_integration/client.py)
