# MCP Architecture — AIAAS

**Model Context Protocol (MCP)** is a standard that lets AI models call tools running in external processes. In AIAAS, every MCP server is a live process (or remote HTTP endpoint) that exposes a named set of tools. The platform brokers all interactions: credential injection, session pooling, caching, and access control.

---

## How It Works End-to-End

```
User workflow
    │
    ▼
MCPToolNode.execute()          ← nodes/mcp_integration/nodes.py
    │
    ▼
MCPClientManager.call_tool()   ← mcp_integration/client.py
    │   resolves server config + user visibility
    │
    ▼
CredentialInjector.resolve()   ← mcp_integration/credential_injector.py
    │   decrypts API keys from the credential vault
    │   materialises env vars (stdio) or HTTP headers (SSE)
    │
    ▼
_session()  — connection pool
    │   stdio → spawns subprocess + MCP handshake
    │   SSE   → TCP connect + HTTP upgrade + MCP handshake
    │
    ▼
session.call_tool(tool_name, args)  ← MCP SDK
    │
    ▼
_serialise_tool_result()  → JSON-safe dict back to the node
```

---

## The Five Components

### 1. MCPServer Model (`models.py`)

A database row that describes one server. Key fields:

| Field | Purpose |
|---|---|
| `type` | `stdio` (subprocess) or `sse` (HTTP stream) |
| `command` / `args` | Executable + arguments for stdio servers |
| `url` | Endpoint for SSE servers |
| `env` | Non-secret environment variables (e.g. `NODE_ENV=production`) |
| `required_credential_types` | List of credential slugs that must exist before any tool call |
| `credential_env_map` | Maps `ENV_VAR_NAME → "slug:field"` for stdio injection |
| `credential_header_map` | Maps `Header-Name → "Bearer {slug:field}"` for SSE injection |
| `user` | `NULL` = system-wide (visible to everyone); set = user-private |

**Adding a new server via the API:**

```http
POST /api/mcp/servers/
{
  "name": "PostgreSQL",
  "type": "stdio",
  "command": "npx",
  "args": ["-y", "@modelcontextprotocol/server-postgres"],
  "required_credential_types": ["postgres_url"],
  "credential_env_map": {
    "POSTGRES_URL": "postgres_url:connection_string"
  }
}
```

---

### 2. MCPClientManager (`client.py`)

The single object callers use for both `list_tools` and `call_tool`. It hides pooling entirely from the caller.

```python
manager = MCPClientManager(server_id=7, user=request.user)

# List available tools (Redis-cached for 120 s)
tools = await manager.list_tools()

# Call a tool
result = await manager.call_tool("query", {"sql": "SELECT 1"})
```

**Connection pool:** One live `ClientSession` per `(server_id, user_id)` pair, kept for 300 s. Starting a stdio server costs ~100–500 ms (subprocess + handshake); the pool pays that cost once, not on every tool call. Sessions are **not** concurrency-safe, so an `asyncio.Lock` per entry serialises concurrent callers.

**Session lifecycle:**

```
first call     → _evict old (if any) → open transport → MCP handshake → _PoolEntry stored
subsequent calls → reuse session, refresh TTL
error mid-call → session evicted; next call gets a fresh one
shutdown       → drain_pool() closes all subprocesses cleanly
```

---

### 3. CredentialInjector (`credential_injector.py`)

Translates the server's mapping config into concrete env vars and HTTP headers using the user's encrypted credential vault.

**Mapping syntax (env map):**
```json
{ "GITHUB_TOKEN": "github_token:api_key" }
```
Reads the `api_key` field from the user's `github_token` credential, sets `GITHUB_TOKEN=<decrypted value>` in the subprocess environment.

**Mapping syntax (header map) — supports template strings:**
```json
{ "Authorization": "Bearer {github_token:api_key}" }
```
The `{slug:field}` placeholder is replaced inline. Useful for Bearer tokens, API keys in custom headers, etc.

**Security guarantees:**
- Credentials are decrypted only inside `CredentialInjector.resolve()`, immediately before transport open.
- Decrypted values never enter Django logs, are never returned to the frontend, and never leave the server process boundary.
- `CredentialInjector.validate()` is a dry-run that returns error strings — used by workflow pre-flight and the `/validate_credentials` API endpoint.

---

### 4. MCPToolCache (`tool_cache.py`)

A thin Redis wrapper for `list_tools` responses. Tool schemas change rarely; fetching them fresh on every chat message or palette open would spin up subprocesses unnecessarily.

| Behaviour | Detail |
|---|---|
| TTL | 120 seconds |
| Key | `mcp_tools:v2:{server_id}:user:{user_id}` |
| Invalidation | Called automatically on server update/delete via the ViewSet |
| Fallback | Cache miss → live fetch from the server; cache errors are logged and suppressed |

---

### 5. MCPToolNode (`nodes.py`)

The workflow node type that wires MCP into the LangGraph execution engine.

Config fields:
- `server_id` — which server to connect to
- `tool_name` — which tool on that server
- `arguments` — static JSON args (merged with and overridden by upstream node output)

At runtime, `input_data` from the previous node is merged into `arguments` before the call. The result is normalised to `{"result": ...}` if it isn't already a dict, then passed downstream.

---

## API Endpoints

All routes are under `/api/mcp/servers/`.

| Method | Path | What it does |
|---|---|---|
| `GET` | `/api/mcp/servers/` | List servers visible to the authenticated user |
| `POST` | `/api/mcp/servers/` | Create a new user-owned server |
| `GET` | `/api/mcp/servers/{id}/` | Retrieve a single server |
| `PUT/PATCH` | `/api/mcp/servers/{id}/` | Update (owner only) |
| `DELETE` | `/api/mcp/servers/{id}/` | Delete (owner only) |
| `GET` | `/api/mcp/servers/{id}/tools/` | List tools for this server (cached) |
| `GET` | `/api/mcp/servers/{id}/validate_credentials/` | Dry-run credential check |
| `GET` | `/api/mcp/servers/tools/` | Aggregate tools from all visible servers |

System-wide servers (`user=NULL`) are **read-only** via the API. They can only be created or modified through Django admin or migrations.

---

## Workflow Pre-flight Validation (`workflow_validator.py`)

Before `engine.run_workflow()` is called, `assert_mcp_nodes_valid()` scans the workflow JSON for `mcp_tool` nodes and validates every referenced server:

1. Server exists and is visible to the user.
2. Server is enabled.
3. All required credentials are present and decryptable.

Errors are **gathered, not fail-fast** — the user sees every missing credential in one response rather than fixing them one by one.

```python
# Called in the execution entrypoint before graph.ainvoke()
await assert_mcp_nodes_valid(workflow_json, user)
```

---

## Visibility Rules

| Server `user` field | Who can see it |
|---|---|
| `NULL` | All authenticated users (system-wide) |
| `user_id=42` | Only user 42 |

The ViewSet `get_queryset` enforces this:
```python
MCPServer.objects.filter(Q(user=request.user) | Q(user__isnull=True))
```

The `MCPClientManager` re-checks visibility at connection time via `get_visible_server_for_user()`, so a race condition (server deleted mid-execution) raises `PermissionDenied` rather than silently succeeding.

---

## Configuring a Real Server — Step by Step

### Example: Playwright browser automation (stdio)

**1. Store the credential** (Settings → Credentials):
- Type slug: `playwright_key`
- Field: `api_key`

**2. Create the MCP server:**
```http
POST /api/mcp/servers/
{
  "name": "Playwright Browser",
  "type": "stdio",
  "command": "npx",
  "args": ["-y", "@playwright/mcp"],
  "required_credential_types": ["playwright_key"],
  "credential_env_map": {
    "PLAYWRIGHT_API_KEY": "playwright_key:api_key"
  },
  "setup_notes": "Requires Node 18+. Install playwright globally first."
}
```

**3. Verify:**
```
GET /api/mcp/servers/{id}/validate_credentials/
→ {"ok": true, "errors": []}

GET /api/mcp/servers/{id}/tools/
→ {"tools": [{"name": "navigate", ...}, {"name": "click", ...}]}
```

**4. Use in a workflow:**  
Add an `MCP Tool` node → select `Playwright Browser` → select `navigate` → set `{"url": "https://example.com"}`.

---

### Example: Remote SSE server with Bearer auth

```http
POST /api/mcp/servers/
{
  "name": "My Remote Tools",
  "type": "sse",
  "url": "https://tools.internal/mcp/sse",
  "required_credential_types": ["internal_api"],
  "credential_header_map": {
    "Authorization": "Bearer {internal_api:token}"
  }
}
```

---

## Debugging

**Credential errors:**
```
GET /api/mcp/servers/{id}/validate_credentials/
→ {"ok": false, "errors": ["MCP server 'X' requires a 'postgres_url' credential."]}
```
Go to Settings → Credentials and add the missing credential type.

**Connection failures** (`502 Bad Gateway` from the tools endpoint):
- Check that `command` is on `PATH` (e.g. `which npx`).
- For SSE, check network reachability to `url`.
- Inspect Django logs for the full traceback — `MCPClientManager._connect_stdio` / `_connect_sse` log exceptions at `ERROR` level.

**Stale tool list:**
- Tools are cached for 120 s. Updating a server auto-invalidates the cache.
- To force a live fetch: `PUT /api/mcp/servers/{id}/` (triggers invalidation) or wait for TTL expiry.

**Pool leak on dev restart:**
- `drain_pool()` in `mcp_integration/apps.py` (AppConfig teardown) closes all subprocesses. If you kill the dev server hard (SIGKILL), orphan subprocesses may linger — kill them manually or restart the shell.

---

## Source Map

| File | Role |
|---|---|
| [mcp_integration/models.py](../mcp_integration/models.py) | `MCPServer` schema |
| [mcp_integration/client.py](../mcp_integration/client.py) | Connection pool + `MCPClientManager` |
| [mcp_integration/credential_injector.py](../mcp_integration/credential_injector.py) | Credential resolution + injection |
| [mcp_integration/tool_cache.py](../mcp_integration/tool_cache.py) | Redis tool-list cache |
| [mcp_integration/nodes.py](../mcp_integration/nodes.py) | `MCPToolNode` — workflow integration |
| [mcp_integration/workflow_validator.py](../mcp_integration/workflow_validator.py) | Pre-execution validation |
| [mcp_integration/views.py](../mcp_integration/views.py) | REST API (ViewSet + custom actions) |
| [mcp_integration/serializers.py](../mcp_integration/serializers.py) | DRF serializer |
