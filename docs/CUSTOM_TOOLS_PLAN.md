# Custom Tools Plan — user-created tools: private by default, shareable like skills/agents

Status: **APPROVED — implementing.** Owner clarification (2026-09-23):
a tool may be shared like a skill or an agent; sharing an agent shares its
tools automatically; **credentials are never shared**. §1 is now the contract.

---

## 1. Requirement (confirmed)

1. The Tools page header offers **"New agent"**; it must offer **"New tool"**
   instead — creating an agent from the tool library is a context error.
2. A user can **create their own tools** (their REST API, their database —
   a connection row executed through the existing generic callers, not code
   they write).
3. A tool is **private by default**: visible and callable only by its owner.
4. A tool may be **shared like a skill or an agent**: the same
   `link < platform < public` visibility ladder, snapshot-not-pointer
   listings, install counts.
5. **Sharing an agent shares its tools automatically**: installing a shared
   agent installs private copies of the custom tools it needs — the installer
   is never left with an agent pointing at nothing.
6. **Credentials never travel.** A shared or installed tool carries no secret
   and no `secret_ref`; the installer links their own vault credential before
   the copy can call anything authenticated.

## 2. What the platform already has (so the plan reuses, not rebuilds)

| Capability | State | How it maps to "custom, private tools" |
|---|---|---|
| Custom MCP servers | **Shipped.** Connections → Advanced lets a user add their own MCP server row (`MCPServer.user` set); its tools are namespaced per server and only that user can see/call them | Already "user-created, user-private tools" for anything with an MCP server |
| Generic API caller + SQL tools | **In tree, uncommitted (P4).** `datasources.ApiConnection` / `DataConnection` (both `user`-scoped), executed through `call_api` / `query_sql` with vault auth, egress guard, read-only SQL parsing | "User-created, user-private tools" for REST APIs and databases — **but with no UI and no HTTP CRUD** (rows only via Django admin) |
| Tool library page | **Shipped.** Code-owned catalogue + per-user on/off overlay (`ToolConfig`) | Knows only code tools; custom rows have nowhere to appear |
| Grants + scopes | **Shipped.** `data` / `api` / `mcp` grants, per-agent connection scoping | The permission side already exists wherever connections exist |

So the honest gap is narrow: **creation and management UX for private
connections**, plus the **"New tool" entry point** — not a new execution
engine. (User-authored *code* tools were explicitly deferred in
`TOOL_CONFIGURATION_PLAN.md`: code review, schema builder, effect
classification, second permission model. That decision stands unless §1 says
"tool" means "code I write".)

## 3. Interpretation (locked)

**A custom tool = a connection row** (API or Database), created by the user,
executed through the existing generic callers, governed by the existing
grants — **plus sharing**: published like an agent (snapshot, visibility,
install), where a listing is a frozen projection and installing is what makes
a private working copy. "New tool" opens a creation dialog; a "My tools"
section on the Tools page lists, edits, deletes and shares them.

## 4. Design rules

1. **Private by construction, not by filter.** Every read and write filters
   `user=request.user`. A foreign id answers **404, never 403** (a 403 for
   "exists but not yours" is an ownership oracle). Dispatch re-checks
   ownership per call (`ApiConnection.objects.filter(id, user_id)`), so a
   stale or forged id dies at runtime, not just in the UI.
2. **Secrets by reference, never by value — and never travelling.** The form
   picks a vault credential; the row stores `secret_ref` (`slug.field`)
   only. Raw tokens never touch this API, a `secret_ref` naming another
   user's slug is refused, and resolution happens at dispatch, after approval
   (`credentials/refs.py`). A published snapshot carries the auth *shape*
   (type, header name) but no reference and no value; an installed copy starts
   unauthenticated until its owner links a credential.
3. **No new execution surface.** Creation and install write rows the existing
   tools already read. Validation at write is the whole new backend:
   egress-denied hosts refused (SSRF check first), methods restricted to a
   known set, OpenAPI spec capped (~500 KB, operation index only), SQL kinds
   limited to the four driver kinds, `name` unique per user.
4. **A listing is a snapshot, not a pointer** (the `SharedAgent` rule).
   `SharedTool` freezes `tool_config` + `auth_shape` at publish; rendering the
   author's live row would let them widen what installers receive without
   re-consent. Withdrawing unlists rather than deletes; installs already made
   keep working.
5. **Sharing an agent carries its tools.** `to_shareable` already strips ids
   into requirements; `apiConnections` / `dataConnections` ids become
   requirements of new kinds (`api_tool`, `data_tool`) that embed the tool
   snapshot. Install auto-creates the installer's private copies and maps the
   new agent's scopes onto them — then surfaces one credential requirement
   per tool needing auth, answered from the installer's own vault. Fail
   (don't drop) when a referenced tool no longer resolves.
6. **The library stays honest.** The code-owned catalogue section is
   untouched; custom tools render in their own "My tools" section so a
   connection is never confused with a built-in.
7. **Grants still govern.** A custom tool is *reachable* only through the
   matching grant (`api` / `data`) on the agent, or chat's own toolbox. The
   runtime default (no scope set) is "any connection this user owns", so tools
   work before per-agent scoping UI exists.

## 5. Phases

### Phase A — Backend CRUD (private tools work end to end)

- `datasources/serializers.py`: `DataConnectionSerializer`,
  `ApiConnectionSerializer` + §4 rule-3 validation.
- `datasources/views.py`: two `ModelViewSet`s, `IsAuthenticated`,
  `get_queryset()` scoped to the caller, `perform_create()` stamps
  `request.user`.
- `datasources/urls.py`, wired as `/api/datasources/data-connections/` and
  `/api-connections/` in `workflow_backend/urls.py`.
- Tests `datasources/tests/test_api.py`: cross-user isolation (read, update,
  delete → 404), every validation refusal, foreign `secret_ref` refused,
  per-user name uniqueness.
- Update `Backend/docs/API.md` in the same change (repo rule).

### Phase B — Sharing backend (tools travel, credentials don't)

- `datasources/models.py::SharedTool`: author, slug, name, tagline,
  `tool_kind` (`api`|`data`), frozen `tool_config` + `auth_shape` (no
  reference, no value), `visibility` (`link < platform < public`,
  default `platform`), `is_listed`, `version`, `install_count`.
  Source row `SET_NULL` so deleting your own tool never retracts installs.
- Publish / withdraw / install endpoints; install writes a private row owned
  by the installer with empty auth and returns the credential requirement.
- `agents/publishing.py`: `api_tool` / `data_tool` requirement kinds with
  embedded snapshots; gallery install auto-creates copies, maps scopes, and
  appends credential requirements answered from the installer's vault.
- Tests: snapshot strips secrets, install copies work unauthenticated-only,
  agent install auto-creates tools, withdrawn listing still installs.
- `API.md` rows in the same change.

### Phase C — Frontend

- `src/api/datasources.ts`: typed CRUD + share/install client for both
  resources; credential-picker options from the existing credentials API.
- Tools page header: **"New agent" → "New tool"** dialog (API / Database
  tabs: name, base URL or kind/host/database, auth picker, allowed methods or
  `allow_write`, OpenAPI paste with operation-count preview).
- **"My tools"** section: list (name, kind, target, auth state, visibility),
  edit, delete-with-confirm, share (visibility + link), install-from-link.
- Vitest coverage for dialog validation and section rendering.

### Phase D — Verification

- `pytest datasources`, agent sharing tests, `npx tsc --noEmit`, `eslint`,
  frontend suite.
- Manual: create → share → install as a second user (tool works only after
  linking own credential); publish agent using a custom tool → install →
  tool copy arrives with the agent; confirm no secret travelled at any step.

## 6. Explicitly out of v1

- Agent-builder connection pickers (`dataConnections` / `apiConnections`
  scoping UI) — runtime works without it.
- Python/code custom tools — needs its own plan.
- Server-side OpenAPI-URL import (paste spec text in v1).
