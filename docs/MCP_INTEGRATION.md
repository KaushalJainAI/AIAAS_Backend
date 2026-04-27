# MCP Integration Architecture

The **Model Context Protocol (MCP)** is the backbone of the AIAAS tool system. It allows the platform to connect to local or remote servers that provide specialized tools (like Browser Automation, SQL query execution, or Google Search) and expose them to the AI.

## 1. Technical Components

### A. MCP Client Manager (`client.py`)
This is the low-level transport layer.
- **Connection Pooling**: To avoid the 500ms overhead of spawning subprocesses for every tool call, the manager pools `ClientSession` objects per `(server_id, user_id)` for 5 minutes.
- **Transports**: Supports both **Stdio** (local subprocesses) and **SSE** (remote HTTP streams).
- **Concurrency**: MCP sessions are not thread-safe; the manager uses `asyncio.Lock` to serialize access to shared sessions.

### B. Credential Injector (`credential_injector.py`)
This is the "Security Bridge" between the user's encrypted vault and the MCP server.
- **Environment Mapping**: Injects credentials directly into the environment variables of a `stdio` process.
- **Header Mapping**: Injects credentials into the HTTP headers of an `SSE` stream.
- **Resolution**: Dynamically resolves placeholders like `{google:api_key}` into actual decrypted values just-in-time for the connection.

### C. Tool Provider & Cache (`tool_provider.py` / `tool_cache.py`)
- **Aggregation**: Combines tools from all enabled servers (User-owned + System-wide) into a single registry for the LLM.
- **Caching**: Tool definitions (names, descriptions, schemas) are cached in Redis to prevent excessive "ListTools" calls.

## 2. Integration Flow

1.  **User Configures Server**: A user adds an MCP server (e.g., `npx -y @modelcontextprotocol/server-postgres`) and defines which credential types it needs.
2.  **Compilation/Validation**: The `WorkflowValidator` checks if the user possesses the required credentials before the workflow is even allowed to start.
3.  **Tool Execution**:
    - The LLM requests a tool call.
    - The `MCPClientManager` fetches/creates a pooled session.
    - The `CredentialInjector` decrypts the user's secrets and applies them to the session transport.
    - The tool is executed, and the result is serialized into JSON for the AI.

## 3. Security Hardening

- **User Scoping**: MCP servers can be marked as "User Owned," meaning only that user's process can see or call those tools.
- **Zero-Exposure Policy**: Credentials are only decrypted inside the `CredentialInjector` during the handshake phase. They are never returned to the frontend or exposed in logs.
- **Subprocess Isolation**: `stdio` servers run as independent subprocesses, ensuring that if a server crashes, it doesn't take down the Django backend.

---

**Source Reference**: [mcp_integration/client.py](file:///c:/Users/91700/Desktop/AIAAS/Backend/mcp_integration/client.py)
