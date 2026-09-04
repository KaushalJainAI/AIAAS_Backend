# Retiring workflows

> ** Deprecated — decision executed 2026-08-17, kept as a historical record.**
> The retirement this doc decides was carried out: the DAG product surface is
> gone and agents are the whole product. The four DAG-era tables
> (`WorkflowVersion`, `WorkflowTestResult`, `WorkflowCloneHistory`,
> `TriggerState`) were dropped 2026-08-18.

Written 2026-08-14. Decision: the DAG product surface is being removed; agents
become the whole product.

> **Read §1 before deleting anything.** Most of what looks like "workflow code"
> is the runtime every agent turn depends on. A retirement done by deleting
> things whose names contain "workflow" or "node" takes the agent product down
> with it.

## 1. What must survive

`chat/turn/llm.py:552` and `:615` invoke every model like this:

```python
result = await handler.execute({}, request.config, _execution_context(user_id))
```

**Every LLM provider is a workflow node handler.** Agents call models through
the node system. So the following are not workflow code — they are the agent
runtime, filed under workflow-sounding names:

| Must survive | Lines | Why |
|---|---|---|
| `compiler/schemas.py` — `ExecutionContext` | 460 | Passed to every handler call, on every agent turn |
| `compiler/validators.py` | 449 | Also backs the live `POST /api/compile/validate/` |
| `compiler/config_access.py` | 59 | Config/credential reading used by handlers |
| `nodes/handlers/base.py` | 518 | `BaseNodeHandler`, the calling convention |
| `nodes/handlers/registry.py` | 244 | `chat/turn/llm.py` calls `get_registry().has_handler()` |
| `llm/handlers/llm_providers.py` | 132 | The four providers |
| `llm/handlers/openai_compatible.py` | 594 | openrouter / nvidia / openai transport |
| `llm/handlers/llm_nodes.py` | 499 | Ollama — the one provider not speaking OpenAI protocol |
| `llm/handlers/llm_base.py` | 382 | Shared SSE/streaming plumbing for the above |
| `mcp_integration/tool_provider.py` | — | Agent MCP tools. **Not** `mcp_integration/nodes.py`, which is the workflow node |
| `agents/models.py::Workflow` | — | **Agents are `Workflow` rows** with `kind='agent'` |
| `agents/models.py::HITLRequest` | — | Agent approvals were bridged onto it during the merge |

The four supported providers are `openrouter`, `nvidia`, `openai`, `ollama`
(`nodes/providers.py::SUPPORTED_PROVIDERS`).

## 2. The chokepoint

`nodes/handlers/registry.py` has **one eager registration function** that imports
every handler in a single block — core, logic, utility, subworkflow,
integration, triggers, langchain, REST connectors, LLM nodes, and the MCP node.
`chat/turn/llm.py:410` calls `get_registry().has_handler(node_type)` on every LLM
call, so that function runs on the agent's hot path.

**Nothing can be deleted until that function is trimmed.** Delete a handler
module first and every agent turn dies on an ImportError. Trim the registry
first and the orphaned modules become genuinely unreferenced.

This is the whole mechanical difficulty of the retirement, and it is one
function.

## 2a. Status — executed 2026-08-17

Done:

- **HTTP surface** — the canvas routes (`executions`, `generation`, `partial`,
  `versions`, deploy/undeploy/test/clone, `background-tasks`, `system/info`,
  `chat/context-aware`) and the three `nodes` schema routes. `nodes` has no
  `views.py` or `urls.py` left.
- **Registry trimmed first**, as §2 requires. `get_registry()` now registers the
  four LLM providers, MCP, and the tool-shaped nodes only.
- **Handlers deleted** — `triggers/` (1649), `logic_nodes` (429), `utility_nodes`
  (229), `core_nodes` (206), `subworkflow_node` (165), `node_loader` (251).
- **Trigger runtime deleted** — `executor/trigger_manager.py` (260), the public
  webhook receiver `agents/views/webhooks.py` (139), `agents/signals.py`,
  `AgentsConfig.ready()`, and the `execute_workflow_async` /
  `execute_scheduled_workflow` / `poll_workflow_trigger` tasks (178).
- **Models** — `CustomNode` dropped for real (`nodes.0006`, zero rows).
- **LLM handlers moved to `llm/handlers/`** (`llm_base`, `llm_nodes`,
  `llm_providers`, `openai_compatible`), joining `llm/providers.py` and the
  `AIProvider`/`AIModel` registry.

**Kept, against this document's original table:** `langchain_nodes.py`,
`integration_nodes.py`, `rest_base.py` + `connectors/`. These are being
*converted* into `chat/tools/` entries, not discarded — deleting them would
throw away the source material for that conversion.

Also done (second pass, same day):

- **King reworked out, then deleted.** Its two remaining callers were already
  inert: `respond_to_hitl` pushed onto `_hitl_responses`, a per-process
  in-memory queue populated only inside a running DAG's `ask_human()`, so it
  always returned `False` while the view returned 200 regardless; and
  `update_settings` set three attributes on a singleton before writing the same
  three `UserProfile` fields. `agents/views/hitl.py` now keeps just its DB
  write, `agents/views/system.py` writes the profile directly.
- **Deleted:** `king.py` (1570), `generation.py` (450), `tasks.py` (296),
  `engine.py` (243), `credential_utils.py` (61), `llm_json.py` (59),
  `hitl.py` (38), `exceptions.py` (38), and `compiler/compiler.py` (588) with
  its `views.py` / `urls.py` / `serializers.py`.
- **Six Celery tasks** went with `tasks.py` — `index_document_async`,
  `cleanup_old_executions`, `cleanup_expired_hitl_requests`,
  `refresh_oauth_tokens`, `send_hitl_notification`, `update_template_metrics`.
  None had a caller and none was in `CELERY_BEAT_SCHEDULE`, which contains only
  `notifications.sweep_hitl_reminders`.  If OAuth refresh or HITL expiry are
  wanted, they must be **rebuilt and scheduled** — they were not running.

**`executor/` is now `sandbox/` and nothing else** — `safe_execution.py` (459)
and `wasm_sandbox.py` (110), reached from `chat/tools/sandbox.py` for the
`execute_python` tool. It is stdlib-only and never depended on the DAG runtime.
`compiler/` keeps `schemas.py` (`ExecutionContext`, built on every LLM call),
`validators.py`, `config_access.py` and `node_types.py`.

Still open:

- **`mcp_integration/nodes.py`** (`MCPToolNode`) — still registered, still
  optional; agent MCP goes through `tool_provider.py` instead.

Done 2026-08-18:

- **The four DAG-era tables are gone.** `WorkflowVersion`,
  `WorkflowTestResult`, `WorkflowCloneHistory` and `TriggerState` were dropped
  by `orchestrator.0014_drop_dag_era_tables`, along with the model classes,
  `WorkflowVersionSerializer`, the admin registrations, and the version
  snapshot written on PUT in `workflow_detail`. The `Workflow` table itself and
  its `nodes` / `edges` / `viewport` / `workflow_settings` columns stay —
  `kind='workflow'` rows are still CRUD'd over the API.

## 3. What goes

**Backend — node handlers** (delete only after §2):

| File | Lines |
|---|---|
| `nodes/handlers/triggers/` (was `triggers.py`, 1519 lines) | 1602 across 6 modules |
| `nodes/handlers/langchain_nodes.py` | 917 |
| `nodes/handlers/logic_nodes.py` | 429 |
| `nodes/handlers/rest_base.py` + `connectors/` | 364+ |
| `nodes/handlers/node_loader.py` | 251 |
| `nodes/handlers/utility_nodes.py` | 229 |
| `nodes/handlers/integration_nodes.py` | 206 |
| `nodes/handlers/core_nodes.py` | 206 |
| `nodes/handlers/subworkflow_node.py` | 165 |
| `mcp_integration/nodes.py` | — |

**Backend — execution:** `compiler/compiler.py` (588), `executor/engine.py`
(243), and `executor/king.py` (1570) — see §5 for the King caveat.

**Backend — models** (migration): `WorkflowVersion`, `WorkflowTestResult`,
`WorkflowCloneHistory`, `TriggerState`. `Workflow` itself **stays**.

**Frontend:** `WorkflowEditor.tsx` (~1500), `WorkflowsDashboard.tsx`, the
`components/workflow/` DAG-authoring parts (`NodePanel`, `NodeConfigPanel`,
`GenericNode`, `DataMappingPanel`, `ExpressionEditor`, `NodeBuilderModal`,
version history, import/export), `lib/validateWorkflow.ts`, `stores/useGraphStore.ts`.

**Frontend that must stay** — it now serves agents: `lib/executionEvents.ts`,
`AgentCanvas.tsx`, `AgentTraceNode.tsx`, `CapabilityPanel.tsx`,
`AgentRunLog.tsx`, `ExecutionOverlay.tsx`.

## 4. What happens to `Workflow` rows

Agents are `Workflow` rows, so the table stays. The `nodes` / `edges` /
`viewport` / `workflow_settings` columns become dead weight for agents but are
harmless; dropping them is optional and can come later.

Existing `kind='workflow'` rows: **decide before the migration runs.** Options
are delete them, or leave them orphaned and unreachable. The dev database has 2.
Production is unknown and must be checked first — this is the one genuinely
irreversible step in the whole plan.

## 5. The King question

`executor/king.py` (1570 lines) is the only remaining importer of the engine, so
it nominally dies with it. But it is not purely a workflow runner — it also
holds `ask_human` / `submit_human_response` / `respond_to_hitl`, health checks,
and LLM settings management.

Two things to check before deleting it, because both are used outside the DAG:

1. Does the **agent** HITL path route through King, or only through
   `agents/views/hitl.py::respond_to_hitl`? The merge bridged agent approvals
   onto `HITLRequest`; if King is in that path, extract before deleting.
2. `AGENT_WORKFLOW_UNIFICATION.md` §4.3 argues supervision becomes *more*
   important under agents, not less. King is the only existing supervisor.
   Deleting it may be discarding something wanted back within a quarter.

**Recommendation: retire King last, as its own decision.** It is not blocking
anything else in this plan.

## 6. Order

Each stage leaves the tree green. Do not reorder — §2 is a hard dependency.

1. **Trim `registry.py`** to the LLM providers only. Nothing deleted yet.
   Verify: full `pytest`, and an agent run still reaches a model.
2. **Delete the orphaned handler modules** (§3) + `mcp_integration/nodes.py`.
   Verify: `pytest`, agent run.
3. **Delete `compiler/compiler.py` + `executor/engine.py`** and the routes that
   drive them (`execute_workflow`, `execute_partial`, pause/resume/stop,
   `test_workflow`, `clone_workflow`, the two dead compiler views). Update
   `docs/API.md` in the same change.
4. **Frontend removal** (§3), route redirects, and drop the Workflows tab from
   `AutomationTabs` so "Automations" means agents.
5. **Migration** for the four dead models + the `kind='workflow'` row decision
   (§4). Check production first.
6. **King** — separate decision (§5).

## 7. Consequences to accept

- **The deterministic "hands" tier disappears.** Agents will do everything
  through tools and MCP; there is no fixed, replayable side-effect layer.
  `AGENT_TEMPLATES.md` §7 ("agents call workflows as tools") becomes
  unimplementable and that doc needs a correction, not just an edit.
- **`AGENT_WORKFLOW_MERGE_PLAN.md` §Stage 3's premise partly inverts.** The
  execution-event reducers were extracted so *both* surfaces could share them;
  with workflows gone they serve agents alone. They stay — they are tested and
  agents depend on them — but the "shared" justification no longer applies.
- **`compiler/` will hold only `schemas` + `validators` + `config_access`** —
  no compiler. Renaming it (to `runtime/`) stops being cosmetic at that point;
  an app called `compiler` containing no compiler is how the next reader wastes
  an hour.
