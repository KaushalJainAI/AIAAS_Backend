# `mcp_integration/`: the Connections page

Connects outside tool servers to the AI. Two kinds of connection:

- **MCP servers**: programs that speak the Model Context Protocol. Their tools
  are discovered at runtime and named `mcp__<server>__<tool>`. They run as a
  local process (`stdio`) or over HTTP.
- **Native connectors** (`type='native'`): Gmail, Drive, Sheets, Calendar,
  Notion. Their tools are our own code in `chat/tools/google/` and
  `chat/tools/notion.py`. The `MCPServer` row just holds the on/off switch and
  the credential it needs.

Design: [`docs/MCP_ARCHITECTURE.md`](../docs/MCP_ARCHITECTURE.md).

## Data (`models.py`)

| Model | What it is |
|---|---|
| `MCPServer` | A connection. `user=NULL` means a shared built-in one everyone sees |
| `MCPServerPreference` | Your personal on/off switch for a shared connection |
| `MCPToolCatalogue` | The last known tool list for a server, so listing tools never has to start it |
| `MCPOAuthClient`, `MCPOAuthFlow`, `MCPOAuthToken` | OAuth login for remote MCP servers |

## Files

| File | What it does |
|---|---|
| `client.py` | Opens connections to MCP servers, keeps a small pool of them, lists and calls tools |
| `tool_provider.py` | Turns MCP tools into tools the AI loop can call |
| `tool_cache.py` | Caches tool lists: Redis first, then the database, then asking the server |
| `credential_injector.py` | Fills in the user's credentials when a server starts (env vars, headers or a temp file) |
| `supervisor.py` | A memory budget for local MCP processes, so they can't crash the server |
| `launch.py` | How a local server process is started |
| `native.py` | Which native connectors are live for a user (switched on *and* credential present) |
| `oauth.py` | OAuth for remote MCP servers |
| `views.py`, `urls.py`, `serializers.py` | `/api/mcp/servers/...` |

## Safety rules

- A local MCP server gets a **short allow-listed environment**, never the
  server's own environment (which holds database passwords and the encryption key).
- **Listing tools never starts a server.** Only calling a tool does.
- In production `MCP_ALLOW_STDIO=False`: local-process servers are refused.

## Tests

`mcp_integration/tests/`: `test_connections.py`, `test_native_connectors.py`,
`test_subprocess_env.py`, `test_supervisor.py`.
