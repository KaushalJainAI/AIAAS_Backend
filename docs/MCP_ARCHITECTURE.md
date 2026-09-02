# MCP Architecture — AIAAS

**Model Context Protocol (MCP)** is a standard that lets AI models call tools running in external processes. In AIAAS, every MCP server is a live process (or remote HTTP endpoint) that exposes a named set of tools. The platform brokers all interactions: credential injection, session pooling, caching, and access control.

This app is the *runtime* surface for MCP. Tools are not declared anywhere: they are minted at runtime from whatever each server advertises, namespaced so they never collide with built-in tools, and merged into the chat agent's and agent runs' tool loops.

---

## How It Works End-to-End

```
Chat message / agent turn
    │
    ▼
chat/tools/__init__.py — get_available_tools()    ← advertises MCP tools
    │   gated by the `mcp` grant, server enabled, credentials present
    ▼
MCPToolProvider.get_openai_tool_descriptors()     ← mcp_integration/tool_provider.py
    │   per-server 5s timeout, all servers queried concurrently
    ▼
MCPToolProvider.execute(name, args, user)         ← resolves mcp__<id>__<tool> binding
    │
    ▼
MCPClientManager.call_tool()                      ← mcp_integration/client.py
    │   re-checks visibility + enabled + user preference at connection time
    ▼
CredentialInjector.resolve()                      ← mcp_integration/credential_injector.py
    │   decrypts keys from the vault; env vars (stdio) or HTTP headers (SSE)
    ▼
_session() — connection pool
    │   stdio → spawns subprocess + MCP handshake
    │   SSE   → TCP connect + HTTP upgrade + MCP handshake
    ▼
session.call_tool(tool_name, args)  ← MCP SDK
    │
    ▼
_serialise_tool_result()  → JSON-safe dict back to the tool loop
```

There is **no workflow node** any more. `MCPToolNode` (`nodes/mcp_integration/nodes.py`) went with the DAG runtime; `MCPToolProvider` is now the only integration surface. MCP tool output also flows through the central backstop in `chat/tools/tool_output.py` like any other tool's output.

---

## The Six Components

### 1. MCPServer + MCPServerPreference (`models.py`)

`MCPServer` is a database row that describes one server. Key fields:

| Field | Purpose |
|---|---|
| `type` | `stdio` (subprocess) or `sse` (HTTP stream) |
| `command` / `args` | Executable + arguments for stdio servers |
| `url` | Endpoint for SSE servers |
| `env` | Non-secret environment variables (write-only over the API) |
| `required_credential_types` | List of credential slugs that must exist before any tool call |
| `credential_env_map` | Maps `ENV_VAR_NAME → "slug:field"` for stdio injection |
| `credential_header_map` | Maps `Header-Name → "Bearer {slug:field}"` for SSE injection |
| `user` | `NULL` = system-wide (visible to everyone); set = user-private |
| `display_name`, `category`, `tagline`, `icon_slug`, `help_url` | Presentation metadata — the Connections catalog is data, not frontend code |

`MCPServerPreference` holds **per-user** enable/disable state for a server. A missing row means "inherit `MCPServer.enabled`", so an untouched account has none. System-wide servers are shared read-only templates: one user turning Gmail off must not turn it off for everyone, so the toggle cannot be expressed by flipping `MCPServer.enabled` — that was the source of the old `PATCH /api/mcp/servers/<id>/ -> 403`. The serializer exposes `effective_enabled` (the server's flag unless the user has an explicit preference row) and the API lists the user's `disabled_server_ids` once per request so N servers cost one extra query.

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

`command` is validated against `ALLOWED_STDIO_COMMANDS` in `serializers.py` — a stdio server is spawned as a subprocess with the user's credentials in its env, so an unrestricted command would be arbitrary code execution by any authenticated user. SSE `url`s are checked against private/link-local addresses (SSRF guard) at registration **and re-checked at every connection** (DNS rebinding).

### 2. MCPClientManager (`client.py`)

The single object callers use for both `list_tools` and `call_tool`. It hides pooling entirely from the caller.

```python
manager = MCPClientManager(server_id=7, user=request.user)

# List available tools (Redis-cached for 120 s)
tools = await manager.list_tools()

# Call a tool
result = await manager.call_tool("query", {"sql": "SELECT 1"})
```

**Connection pool:** One live `ClientSession` per `(server_id, user_id)` pair, kept for 300 s. Starting a stdio server is expensive — `npx -y <pkg>` resolves and installs before the server prints a byte, measured at ~8.5 s for a package that is already in the npm cache — so the pool pays that cost once, not on every tool call. Sessions are **not** concurrency-safe, so an `asyncio.Lock` per entry serialises concurrent callers.

**Each session lives in its own task.** `_SessionWorker` opens the transport, parks on an event, and unwinds its own exit stack when evicted. This is not tidiness: both transports are built on anyio task groups, and *a task group may only be exited by the task that entered it*. A pooled session is opened by whichever request happened to need it first and evicted by whichever request notices it has expired — almost never the same one — so the close raised

```
RuntimeError: Attempted to exit cancel scope in a different task than it was entered in
```

which `_evict` swallowed, leaving the stdio subprocess running with nothing holding a handle to it. One orphan per eviction, for the life of the process.

**Session lifecycle:**

```
first call       → _evict stale (if any) → spawn worker → transport + handshake → _PoolEntry stored
subsequent calls → reuse session, refresh TTL
error mid-call   → session evicted; next call gets a fresh one
connect failure  → remembered for FAILURE_TTL (60 s); callers get the reason without re-dialling
shutdown         → drain_pool() closes all subprocesses cleanly
```

**Budgets** (`client.py`), split because the callers differ in what they can wait for:

| Constant | Value | Bounds |
|---|---|---|
| `CONNECT_TIMEOUT` | 25 s | Opening a session (spawn + install + handshake) |
| `LIST_TOOLS_TIMEOUT` | 30 s | `GET /tools/` — a person is watching a spinner |
| `AGENT_LIST_TOOLS_TIMEOUT` | 8 s | An agent turn — dead air the user did not ask for |
| `RPC_TIMEOUT` | 15 s | `list_tools` on an open session |
| `CALL_TOOL_TIMEOUT` | 120 s | One tool call |
| `CLOSE_TIMEOUT` | 10 s | Waiting for a worker to unwind before cancelling it |

`drain_pool()` exists and is exercised by tests, but is **not wired into app teardown** — a hard-killed dev server can orphan stdio subprocesses; kill them manually or restart the shell.

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

Field lookup also falls back to a Credential's `access_token`/`refresh_token` columns, where the OAuth flow stores tokens — without that, an OAuth-connected account looked empty to the injector.

**Security guarantees:**
- Credentials are decrypted only inside `CredentialInjector.resolve()`, immediately before transport open.
- Decrypted values never enter Django logs, are never returned to the frontend, and never leave the server process boundary.
- `CredentialInjector.validate()` is a dry-run that returns error strings — used by the `/validate_credentials` API endpoint.

### 4. MCPToolCache (`tool_cache.py`)

A thin Redis wrapper for `list_tools` responses. Tool schemas change rarely; fetching them fresh on every chat message or palette open would spin up subprocesses unnecessarily.

| Behaviour | Detail |
|---|---|
| TTL | 120 seconds |
| Key | `mcp_tools:v2:{server_id}:user:{user_id}` |
| Invalidation | `perform_update`, `perform_destroy`, `set-enabled` and the preference write path all invalidate |
| Fallback | Cache miss → live fetch from the server; cache errors are logged and suppressed |

### 5. MCPToolProvider (`tool_provider.py`)

The bridge between MCP servers and the platform's tool loops, and the only consumer of the pool on the agent path.

Tool names are namespaced so MCP tools never collide with built-in tools:

```
mcp__<server_id>__<tool_name>_<sha1-digest-8>
```

- **`get_openai_tool_descriptors(user, server_ids=None)`** — OpenAI-format function specs for every tool on every enabled server visible to the user, queried concurrently with a 5 s per-server timeout (`asyncio.wait_for`), so a hung or absent stdio server cannot stall a whole agent turn. A server with a missing credential is **skipped entirely** — an advertised tool that cannot run is worse than one never offered. `server_ids` narrows the set to a chosen few connections: chat passes `None` (a human is typing and watching, so the whole workspace is the right scope), while an agent run passes its own `agent_context['connectors']` selection, because an agent is configuration that runs unattended and "every connection this account owns" is not a blast radius anyone chose. The narrowing is applied *after* `get_servers_for_user`, so a stale id can only ever take tools away.
- **`execute(name, arguments, user)`** — resolves the namespaced name back to its server and original tool name, calls it, and returns a string (JSON-encoded for structured payloads) so it plugs directly into the chat tool loop, which expects `str` results. Credential and permission failures become structured error JSON with `code`s (`credential_missing`, `credential_invalid`, `tool_not_found`, `tool_error`) — the model can explain, but the loop never crashes.

`chat/tools/__init__.py` advertises MCP tools only when the `mcp` grant is held (agents) / when the MCP capability is on (chat), and a failure inside `mcp_integration` costs the user their MCP tools, not the built-ins — the registration loop isolates the two.

### 6. Permission policy (`chat/tools/permissions.py`)

`SENSITIVE_TOOLS` holds built-ins and could never hold MCP tools — their names are minted at runtime from a third-party catalogue. So a credentialed MCP call is gated unless its name begins with a verb that only reads (`get_`, `list_`, `read_`, `search_`, `fetch_`, `query_`). The allowlist is that way round on purpose — guessing "write" costs a click, guessing "read" sends the email. Reads are exempt in chat because a human wrote the message and is watching; `unattended_policy` withdraws that exemption for agent runs.

---

## API Endpoints

All routes are under `/api/mcp/servers/` (`mcp_integration/urls.py`).

| Method | Path | What it does |
|---|---|---|
| `GET` | `/api/mcp/servers/` | List servers visible to the authenticated user (own + system-wide), with `effective_enabled` |
| `POST` | `/api/mcp/servers/` | Create a new user-owned server |
| `GET` | `/api/mcp/servers/{id}/` | Retrieve a single server |
| `PUT/PATCH` | `/api/mcp/servers/{id}/` | Update (owner only) |
| `DELETE` | `/api/mcp/servers/{id}/` | Delete (owner only) |
| `POST` | `/api/mcp/servers/{id}/set-enabled/` | Turn a connection on/off for the current user (system → preference row; owned → row's own `enabled`) |
| `GET` | `/api/mcp/servers/{id}/tools/` | List tools for this server (cached, 5 s timeout; 400 `credential_missing`/`credential_invalid`, 502 unreachable) |
| `GET` | `/api/mcp/servers/{id}/validate_credentials/` | Dry-run credential check → `{ok, errors}` |
| `GET` | `/api/mcp/servers/tools/` | Aggregate tools from all visible servers with `server_id`/`server_name` tags ( no overall timeout — flagged in the API.md audit) |

Ownership rules on mutation:
- **System-wide servers (`user=NULL`) are read-only config** — any PATCH on one 403s, **except** a request whose body is exactly `{enabled}`: that is a per-user preference write and returns 200. `{enabled, command}` together still 403s (the toggle must not smuggle config edits past the guard).
- `set-enabled` works uniformly for system and owned servers, which is what lets the UI render one toggle without knowing who owns the row.

---

## Credential Readiness — Where It Is Actually Checked

The DAG-era `workflow_validator.py` (`validate_mcp_nodes` / `assert_mcp_nodes_valid`) scanned workflow JSON for `mcp_tool` nodes before execution. It was deleted 2026-08-19 along with the last of the DAG surface — the pre-flight role died with that runtime, and `get_visible_server_for_user` went with it. Credential readiness is instead checked in the two places that matter:

1. **At advertisement** (`get_openai_tool_descriptors`): a server whose required credentials are missing or invalid is skipped, so its tools never appear in the model's function list.
2. **At execution** (`MCPToolProvider.execute`): a credential that disappeared between turns becomes structured error JSON, not an exception — the model sees `credential_missing` and can say so.

---

## Visibility Rules

| Server `user` field | Who can see it |
|---|---|
| `NULL` | All authenticated users (system-wide) |
| `user_id=42` | Only user 42 |

`_visible_servers_queryset` (in `client.py`) enforces both halves: system + own rows, `enabled=True`, **minus** servers the user has explicitly turned off via `MCPServerPreference` — without the exclude, the Connections toggle would be cosmetic and a "disabled" server would keep advertising its tools on every agent turn. `MCPClientManager` re-checks visibility at connection time (`get_server_config`), so a server deleted mid-execution raises `PermissionDenied` rather than silently succeeding.

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

**4. Use it:** enabled servers' tools are automatically available to the chat agent, and to any agent holding the `mcp` grant — no workflow wiring needed. The model sees them as `mcp__<id>__navigate_<digest>` etc.

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
Go to Settings → Credentials and add the missing credential type. While it is missing, the server's tools will not be advertised to the agent at all.

**Connection failures** (`502 Bad Gateway` from the tools endpoint):
- Check that `command` is on `PATH` (e.g. `which npx`).
- For SSE, check network reachability to `url`.
- Read the error the API returned. `GET /api/mcp/servers/{id}/tools/` reports the child process's own stderr (`_StderrTap`), so a connector pointing at a package that does not exist says `npm error 404 Not Found`, not `TimeoutError`. Failures arrive from anyio as `ExceptionGroup("unhandled errors in a TaskGroup")`; `_describe` flattens that to the leaf, which is why the message names a cause at all.
- A failed connect is cached for 60 s. When retrying a fix, either wait it out or restart the process — otherwise the second attempt replays the first one's error without dialling.
- Inspect Django logs for the full traceback — `MCPClientManager._connect_stdio` / `_connect_sse` log the failure with `exc_info`.

**Stale tool list:**
- Tools are cached for 120 s. Updating, deleting, or toggling a server auto-invalidates the cache.
- To force a live fetch on an owned server: `PUT /api/mcp/servers/{id}/` (triggers invalidation) or wait for TTL expiry.

**Tool call results:**
- A failed call comes back as JSON with a `code` field: `credential_missing`, `credential_invalid`, `tool_not_found`, `tool_error` — check the assistant's explanation, or call the tool directly via `/api/chat/execute-tool/` (it consults the same permission policy).

**Pool leak on dev restart:**
- `drain_pool()` in `client.py` closes all subprocesses, but it is not wired to app teardown any more. If you kill the dev server hard (SIGKILL), orphan subprocesses may linger — kill them manually or restart the shell.

---

## Source Map

| File | Role |
|---|---|
| [mcp_integration/models.py](../mcp_integration/models.py) | `MCPServer` + `MCPServerPreference` schema |
| [mcp_integration/client.py](../mcp_integration/client.py) | Connection pool + `MCPClientManager` |
| [mcp_integration/tool_provider.py](../mcp_integration/tool_provider.py) | Runtime tool minting + dispatch into the agent tool loops |
| [mcp_integration/credential_injector.py](../mcp_integration/credential_injector.py) | Credential resolution + injection |
| [mcp_integration/tool_cache.py](../mcp_integration/tool_cache.py) | Redis tool-list cache |
| [mcp_integration/serializers.py](../mcp_integration/serializers.py) | DRF serializer; `ALLOWED_STDIO_COMMANDS`, SSE SSRF guard, `effective_enabled` |
| [mcp_integration/views.py](../mcp_integration/views.py) | REST API (ViewSet + custom actions) |
| [chat/tools/permissions.py](../chat/tools/permissions.py) | Read-verb allowlist for credentialed MCP calls |
