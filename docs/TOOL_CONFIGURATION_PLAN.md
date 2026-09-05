# Tool Configuration + Overview/Inbox Unification — Plan

> Goal: make tools configurable at runtime without reintroducing DAG / n8n complexity, and unify Overview + Inbox into one functional surface (kept as **Overview** per user decision). Principle: `OBJECTIVES.md:4` *simple systems, shipped* — every change reuses an existing mechanism.

## 1. Context verified

**Tools are code today.** `Backend/chat/tools/__init__.py:31` imports 10 modules; each `@tool(schema)` in `chat/tools/registry.py:79` registers `Tool(name,schema,run,requires,sensitive,parallel,effect)` `registry.py:48`. Agent visibility is closed allow-list `agents/agent/runtime.py:42` `GRANT_TOOLS` + `agents/views/agents.py:56` `TOOL_KEYS` (8 keys) + `types/agentConfig.ts:34` `tools:{8 bools}` → `AgentBuilder.tsx:563`. Adding a tool = code change + redeploy. Only runtime tools are MCP `mcp_integration/tool_provider.py:44` `mcp__<id>__<hash>` from `MCPServer` rows.

**Skills != tools.** `skills/models.py:4` `Skill(title,content)` is injected as system prompt `agents/agent/runtime.py:414`, not callable.

**Overview vs Inbox:** `better-n8n-frontend/src/pages/Overview.tsx:10` analytics ordered by *whether it needs a human* (blocked→autonomy→posture→activity→tool mix→failures). `Inbox.tsx:3` is HITL action queue (`HITLRequest` approve/reject) + extraction review tab. Overlap: `Overview.tsx:410` shows top-3 pending linking to `/inbox`; both poll `useHitlPending()` `Sidebar.tsx:80`. Distinct jobs, extra hop.

**Invariant to preserve:** `CLAUDE.md:290` *A subagent is a configuration, not a graph* — no visual DAG editor, no `compiler/` revival.

## 2. Decision A — One functional page: Overview absorbs Inbox

**Keep name `Overview` (`/overview`).** `/inbox` becomes redirect to `/overview` (or `/overview#approvals` anchor) `App.tsx:169`. Remove `Inbox` entry from `Sidebar.tsx:135` `navGroups[Work]`; move its badge `pendingCount` onto `Overview` (`Radar` icon) — single source `useHitlPending()` stays.

**Layout change `Overview.tsx:358`:** new top section `Approvals` before autonomy hero, reusing `Inbox.tsx:55` logic verbatim (no redesign tax):

```
Overview
├─ PageHeader (window control 7/30/90 unchanged)
├─ [if pending>0] Approvals — queue(420px) + detail pane (InBox.tsx:139-247) inline, oldest first, approve/reject/respond + timeLeft/timeAgo
│   └─ extraction review tab stays → move to Documents/Extraction panel, not here
├─ Autonomy hero Overview.tsx:447
├─ Posture tiles  Overview.tsx:479
├─ Activity chart Overview.tsx:509
└─ Capability mix / Repeat failures / Busiest workflows
```

When `pending==0`, approvals section collapses to one-line `You're clear → See runs` (Inbox.tsx:125 pattern) — page never looks empty. When `pending>0`, it is expanded and the blocked-work teaser `Overview.tsx:410` is removed (was duplicate).

**Files:** `App.tsx:161` redirect, `Sidebar.tsx:80/135` badge move + entry removal, `Overview.tsx:276` add `useMutation respond` + `selectedId` state + 140 lines from `Inbox.tsx` (queue+detail), delete `Inbox.tsx` after migration, update `API.md` §5 HITL routes. No new query key — reuses `['hitl']`.

**Why this shape:** `Overview.tsx:4` ordering was already *needs-human first*; embedding full queue makes the ordering actionable in one scroll, zero new concepts. Redirect preserves deep links `Overview.tsx:422 Link to=/inbox`.

## 3. Decision B — Standard tool library + clear Tools vs Connectors/Plugins split

> Revised per review: no user-authored code tools for now. Keep a **curated, code-owned library** where the user configures what is exposed, and make the product vocabulary unambiguous.

### 3.1 Vocabulary — what each word means

| Term | What it is | Where it lives | Who provides the code | Example |
|------|------------|----------------|-----------------------|---------|
| **Tool** | One callable function the model can invoke (`Tool` dataclass `chat/tools/registry.py:48`). Has `schema`, `effect` (`registry.py:43` `read/reversible/irreversible`), `parallel`, `requires`, `sensitive`. | `chat/tools/*.py` (10 modules `__init__.py:31`: `web,knowledge,conversation,agents,sandbox,artifacts,vision,files,internal,clock`) registered via `@tool(schema)` `registry.py:79` | We do — Python in repo, reviewed, tested | `web_search`, `scrape_webpage`, `knowledge_base_search`, `execute_python`, `read_file`, `invoke_subagent` |
| **Connector** | Credential + connection info that lets *any* tool act **as the user** against an external account. Never callable itself. | `credentials/models.py` `Credential` (AES-encrypted `CREDENTIAL_ENCRYPTION_KEY`) + `mcp_integration/models.py:5` `MCPServer.credential_env_map` / `credential_header_map` (`@settings:VAR` platform keys, `access_token`/`refresh_token` for OAuth) | User links their account (OAuth or API key) | Google OAuth credential, Slack bot token, Notion API key |
| **Plugin** (external tool pack) | A third-party MCP server process that, once its connector is linked, advertises its own tools at runtime (`mcp_integration/tool_provider.py:37` `TOOL_PREFIX=mcp__`, `MAX_NAME_LEN=64` + `sha1[:8]`). | `mcp_integration/models.py:5` `MCPServer` (`command/args` vs `url`, `enabled`, `user NULL=curated`) + per-user `MCPServerPreference` | Third party (npm package / SSE endpoint), executed via `mcp_integration/client.py` pooled subprocess | `github-mcp`, `slack-mcp`, `gdrive-mcp` — each exposes `mcp__<id>__<tool>` |

A tool without a connector is self-contained (web search, RAG, sandbox). A tool that *needs* a credential is a plugin tool: the plugin brings the tool, the connector brings the permission. The sentence "enable Slack" must never be ambiguous about which of the two is being toggled — see `mcp_integration/tests/test_connections.py` `effective_enabled` bug (`PATCH 403` when flipping shared `enabled` instead of `MCPServerPreference`).

**Skills stay out of this table.** `skills/models.py:4` `Skill(title,content)` is prompt injected via `agents/agent/runtime.py:414` `build_system_prompt`, not dispatchable. It configures reasoning, not capability.

### 3.2 Standard library — what we ship

No new type enum. The library is the existing `AVAILABLE_TOOLS` `chat/tools/__init__.py:76` (`schemas()` from `registry.py:118`) — today  ~24 built-ins across 8 grant groups `agents/agent/runtime.py:42` `GRANT_TOOLS`:

- `webSearch`: `web_search, deep_research (60k budget), image_search, video_search` `chat/tools/web.py:33`
- `scrape`: `scrape_webpage (6 extractors), read_url` `web.py:227`
- `rag`: `list_knowledge_bases, knowledge_base_search, keyword_search, list_documents, read_document` `knowledge.py:38`
- `codeExecution`: `execute_python` `sandbox.py:30` (`effect=read`, not `sensitive` in chat)
- `fileOps`: `list_files, find_files, read_file, write_file, edit_file, make_directory, delete_file` `files.py:70` (`requires="files"`, VFS `inference/vfs.py`)
- `subAgents`: `invoke_subagent, search_agents` `agents.py:180` (`sensitive+irreversible`)
- `always`: `get_current_time` `runtime.py:82` + `read_tool_output` `conversation.py:352` (`requires="spill"`)
- `mcp`: whole dynamic set from plugins (see 3.1)

`shell` stays `UNSERVED_GRANTS runtime.py:78` — no host shell from an agent.

User configuration for a standard tool is **which agents may use it and with what limits**, not rewriting its code. Concretely:

- **Per-agent grant:** the 8 booleans already in `AgentBuilder.tsx:564` `tools:{webSearch,scrape,fileOps,rag,codeExecution,mcp,subAgents,shell}` ↔ `agents/views/agents.py:56` `TOOL_KEYS` ↔ `AgentToolbox.allowed_names:130`. This stays the primary knob — it is the permissions screen the runtime enforces `runtime.py:11`.
- **Per-tool global config (new, minimal):** `ToolConfig` row per `(user, tool_name)` storing `enabled` (kill-switch, default true), `config JSON` for the few tools that have a knob worth persisting (timeouts/budgets, not behavior). No code, just values; absent row = defaults. This lets a user disable `video_search` globally without touching every agent, or raise `read_url` `READ_URL_CHAR_LIMIT web.py:227` for their workspace. Most tools have no config — row absent is valid.
- **No per-tool per-agent matrix.** Table explosion for N=24; groups already solve the permission story and keep `AgentSerializer.to_config:317` flat `tools:{k:bool}`.

### 3.3 Data model — one small table, no custom tool code

```python
# Backend/tools_config/models.py  (new app `tools_config`, not `agents`)
class ToolConfig(models.Model):
    user = FK(settings.AUTH_USER_MODEL, CASCADE, related_name='tool_configs')
    tool_name = CharField(max_length=64)  # must be in AVAILABLE_TOOLS or plugin prefix mcp__*
    enabled = BooleanField(default=True)
    config = JSONField(default=dict, blank=True)  # validated per tool_name, e.g. {"charLimit":15000}
    updated_at = DateTimeField(auto_now=True)
    class Meta: unique_together=['user','tool_name'], indexes=[('user','enabled')]
```

- Fresh `migrate` yields zero rows → every standard tool uses code defaults (`thresholds.py` caps, tool-module constants). No seed migration, no curated rows to pin (contrast `mcp_integration` curated servers).
- Validation: `tool_name` must be in `chat/tools/__init__.py:76` `AVAILABLE_TOOLS` names or start with `mcp__` (future: allow disabling a single plugin tool without disabling the whole server). `config` keys whitelisted per tool (serializer rejects unknown keys).

Alternative considered and rejected: JSON on `User` or `SubAgent` — drifts, unqueryable, no per-tool audit. Separate table is queryable and keeps `SubAgent` columns clean (they already group `tool_grants/guardrails/agent_context/sandbox` by reader `agents/models.py:68`).

### 3.4 Runtime integration — same door, one filter added

1. **Toolbox filtering** `agents/agent/runtime.py:98` `AgentToolbox`:
   - `allowed_names:130` as today from `GRANT_TOOLS` → then intersect with `ToolConfig.enabled` for that user (absent=enabled). One extra DB fetch per run, cached per user in `tool_cache.py` TTL 60s pattern, invalidated on `ToolConfig` save.
   - `descriptors:160` filters `AVAILABLE_TOOLS` by same set before appending `MCPToolProvider.get_openai_tool_descriptors` (which itself already filters by `MCPServerPreference.effective_enabled`).
   - `dispatch:182` re-checks `allowed_names` (model can name unoffered tool → `_denied`) before `chat/tools/__init__.py:192` `execute_tool`.
2. **Chat parity** `chat/tools/__init__.py:162` `get_available_tools(user_id,...)` applies same `ToolConfig` filter after `_requirement_met:124` (`memory/vision/spill/files`) — so chat and agents see the same catalogue.
3. **Permissions/observability unchanged:** `registry.py:43` `effect` + `parallel` remain code-owned; `permissions.py:155` `default_policy` / `runtime.py:258` `approval_policy_for` / `sensitive_tools_for` + `stream.on_tool_result:733` `AgentStep` logging + `tool_output.py:TOOL_OUTPUT_CHAR_LIMIT` spill all apply without new policy.

### 3.5 Frontend — two places, two jobs, no overlap

**A. Tools library page (new) — `better-n8n-frontend/src/pages/Tools.tsx`**

- Nav: `Build` group `Sidebar.tsx:145` between `Automations` and `Schedules`, icon `Wrench` (reuse `AgentBuilder` tools section icon). Label **Tools**.
- Content: read-only catalogue grouped by grant (`Web`, `Scrape`, `Knowledge`, `Code & Files`, `Delegation`, `System`). Each card shows: name, description (from `Tool.schema`), badges `effect` (`read`=green, `reversible`=amber, `irreversible`=red), `parallel`, `requires`, `sensitive`. Toggle `Enabled` (writes `ToolConfig.enabled`), and where applicable a disclosure with the 1-2 config fields (e.g., `web_search: {charLimit}`) — no code editor, no schema builder.
- Links to usage: "Used by N agents" count via `GET /api/tools/usage/` (derived from `SubAgent.tool_grants`), and deep-link to filtered `Agents` list.
- No creation, no deletion — library is code.

**B. Connectors / Plugins — keep as `Connections` but disambiguate copy**

- Keep route `/connections` `App.tsx:154` (existing redirects from `/connectors`, `/mcp-servers` stay). Rename display label from `Connections` to **Plugins** in `Sidebar.tsx:172` `Data` group if you want the term to match this doc, or keep `Connections` and change subtitle — pick one and keep URL stable.
- Page sections clearly labeled: **Plugins** (MCPServer rows, curated vs custom, `enabled` is `MCPServerPreference.effective_enabled`) and **Connectors** (linked `Credential` rows from `credentials/models.py` + OAuth status). Current `Connections.tsx` already separates raw MCP config behind `Advanced` disclosure — keep that, just add headings so `plugin` vs `connector` never share a toggle.
- No change to `credentials/` flow; `mcp_integration/credential_injector.py` `@settings:VAR` + `access_token` fallback stays.

**C. AgentBuilder stays the grant source** `AgentBuilder.tsx:564` `tools:{8 bools}` unchanged. Add read-only hint per toggle linking to its group's cards in `Tools` ("Configure defaults in Tools"), and warning when a grant's underlying tools are globally disabled via `ToolConfig` (same denied-wording `runtime.py:235`).

### 3.6 Security and simplicity guards kept

- Standard library code review is the guarantee; no user code path to sandbox-escape. `shell` remains unserved, `wasmtime` + `RestrictedPython` claim corrected `CLAUDE.md:439` (`safe_execution.py` is AST denylist + thread swap, `wasm_sandbox.py` has no callers) — no change.
- Plugin install still bounded by `client.py` pooled sessions + timeouts `CONNECT_TIMEOUT 25s` / `LIST_TOOLS_TIMEOUT 30s` / `AGENT_LIST_TOOLS_TIMEOUT 5s` (env `MCP_AGENT_LIST_TIMEOUT`); failed plugin → `code`+`stderr` message via `_StderrTap`, cached 60s negative.
- `ToolConfig` changes invalidate per-user descriptor cache immediately (direct delete + publish) so toggle takes effect on next turn — no 2-min stale `tool_cache.py:53` issue.

## 4. Implementation phases — shippable increments

**Phase 0 (backend slice, ~0.5 day):** `tools_config` app + migration, `ToolConfig` model/serializer, `GET/PUT /api/tools/` (catalogue from `AVAILABLE_TOOLS` + user overlays) + `GET /api/tools/usage/`, cache invalidation, unit tests `tools_config/tests/test_config.py` (unknown tool_name→400, disabled tool withheld from `AgentToolbox.descriptors`).

**Phase 1 (frontend slice, ~0.5 day):** `Tools.tsx` catalogue grouped by grant, `Enabled` toggle + 2 config disclosures, `Sidebar.tsx:145` Build entry, `AgentBuilder.tsx:564` hint+warning, `API.md` § new row `GET /api/tools/`.

**Phase 2 (Overview merge, ~0.5 day):** embed Inbox queue/detail into `Overview.tsx`, redirect `App.tsx:169` `/inbox→/overview`, badge move `Sidebar.tsx:80`, delete `Inbox.tsx` after e2e `scripts/` smoke check.

**Phase 3 (optional):** per-tool config expansion (only if a real limit is hit — e.g., `deep_research` fanout knob), search/filter in Tools, bulk enable/disable. No new backend concepts.

Each phase deployable without the next; Phase 0 alone makes tool availability user-configurable (kill-switch + limits) without any new execution path. Custom tools remain out of scope by decision.

## 5. Alternatives rejected

- **User-authored custom tools (http/python/agent rows):** powerful but violates *standard library* constraint — introduces code review, schema builder, `effect` classification, spill handling, and a second permission model. Deferred; the library + `ToolConfig` covers the stated need with zero new execution surface.
- **Visual DAG / n8n node palette:** recreates `compiler/` retired for unbounded delegation depth/budget/egress reasons `agents/agent/orchestrator.py:40`; violates *configuration, not graph*.
- **Per-tool per-agent grant matrix (N toggles):** table explosion for N≈24; groups already solve permission story and keep `AgentSerializer.to_config:317` flat. `ToolConfig` global kill-switch handles the rare per-tool exception.
- **Merging Tools into Connections:** `imporvements.md:2.3` audit trail shows they answer different questions — *what can it do* (tools, code) vs *what can it do as me* (connectors, credentials, plugin processes). Same page re-creates `mcp-servers`/`connectors` two-views-of-two-tables bug.

## 6. Verification

- `pytest tools_config/tests` + `agents/tests/test_agent_runtime.py::SpendCapTests` + `chat/tests/test_permissions.py` + `mcp_integration/tests/test_connections.py` stay green (new `ToolConfig` fixture uses `update_or_create` — `CredentialType` table not empty `mcp_integration/tests/test_fresh_install.py` pattern).
- Manual: disable `video_search` in Tools → agent with `webSearch` grant no longer lists it in `AgentToolbox.descriptors`; enable again → appears next turn (no stale cache).
- Fresh `migrate` yields zero `ToolConfig` rows, every standard tool available, Overview at `/overview` with zero pending, 302 from `/inbox`.

## 7. Open question

- Plugins nav label: keep `Connections` (URL + existing redirects stable) with subtitle *Plugins & connectors* vs rename display to `Plugins`. Recommend keep `Connections` URL, display `Plugins` — zero migration cost.

