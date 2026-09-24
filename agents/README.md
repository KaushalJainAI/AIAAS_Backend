# `agents/`: saved agents, and everything that runs them

An **agent** here is a saved setup, not code: a prompt, a model, the tools it
is allowed (its *grants*), and safety settings. This app stores agents, runs
them, schedules them, pauses them for approval, and lets people share them.

> **Name trap.** The Python package is `agents`, but Django knows this app as
> **`orchestrator`** (see `apps.py`). So you import `from agents.models import SubAgent`,
> but the database tables are `orchestrator_*`, migrations say
> `('orchestrator', ...)`, and URLs are `/api/orchestrator/...`. This is
> deliberate: renaming the label would need a database migration.

## Read in this order

1. `models.py` → `SubAgent`: what an agent is.
2. `agent/runtime.py` → `GRANT_TOOLS` (top of the file): which tools each grant unlocks.
3. `agent/runtime.py` → `start_agent_run` and `run_agent`: how a run starts and executes.
4. `views/runs.py` → `agent_execute`: the HTTP door that calls them.
   `views/agents.py` → `AgentSerializer`: how an agent is validated and saved.
5. `agent/stream.py`: how a run is recorded while it happens.

## Data (`models.py`)

| Model | What it is |
|---|---|
| `SubAgent` | An agent. `tool_grants` (which tool groups), `guardrails` (autonomy, spend cap...), `agent_context` (knowledge bases, connectors, file access...), `output_schema` (the shape its answer must have) |
| `Trigger` | A schedule, webhook or event that starts an agent. `next_due_at` says when |
| `HITLRequest` | A paused run waiting for a person to answer |
| `SharedAgent` | A published copy of an agent others can install |
| `SchedulerLease` | Makes sure only one process fires schedules at a time |
| `ConversationMessage` | The agent builder's chat history (`views/builder.py`). Normal chat uses `chat.ChatMessage` instead |

The records of *what a run did* live in the `logs` app, not here.

## Files

| File | What it does |
|---|---|
| `agent/runtime.py` | **The runtime.** Grants, autonomy levels, the toolbox an agent gets, `start_agent_run` / `run_agent`. Every run from every source starts here |
| `agent/stream.py` | Saves each turn and tool call as it happens and sends live updates |
| `agent/orchestrator.py` | One agent handing work to others ("delegation"), with limits on depth, budget and result size |
| `agent/hitl.py` | Opens and closes `HITLRequest` rows when a run pauses for approval |
| `agent/tasks.py` | The coding lead starting workers without waiting for them |
| `views/` | HTTP endpoints, one file per area: `agents` (CRUD), `runs`, `triggers`, `hitl`, `gallery` (Explore), `builder` (the builder chat), `capabilities`, `wizard` |
| `views/agents.py` → `AgentSerializer` | The **only** way an agent is saved. The builder, templates and chat (`chat/tools/authoring.py`) all use it, so every save gets the same checks |
| `serializers.py` | The approval-request serializer (`HITLRequestSerializer`) and the agent-name guard |
| `gallery/` | The installable templates: one file per pack, plus `standalone.py`. `__init__.py` joins them and explains the rules |
| `publishing.py` | Turning your agent into a shareable copy, with your private ids removed |
| `contracts.py` | Output shapes an agent can be required to return (`output_schema`) |
| `connector_scope.py` | Which connections, and which of their tools, an agent may use |
| `triggers.py` | Cron parsing, "when does this fire next", and describing a schedule in words |
| `scheduler.py`, `sweep.py`, `tasks.py` | Firing due schedules (in-process loop, shared logic, Celery entry) |
| `recovery.py` | Finding runs whose process died, and resuming or closing them |
| `admission.py`, `budget.py`, `spend.py` | Limits: how many runs at once, how long, how much money |
| `stock.py` | Built-in agent configs |
| `playbooks/` | Markdown instructions shipped with the coding templates |

## Key rules

- **One door.** Every run goes through `start_agent_run` / `run_agent`, whatever
  started it (`caller` = `chat`, `orchestrator`, `trigger`, `api` or `eval`). Don't add a second way.
- **Grants say "whether", scopes say "which".** E.g. the `mcp` grant allows
  connectors at all, and `agent_context['connectors']` says which ones.
  An empty scope means "no restriction", so old agents keep working.
- **Autonomy has five levels:** `plan` (tools that change things are removed) →
  `review` → `ask` → `auto` → `full` (never asks). See `AUTONOMY_LADDER`.
- **Unattended runs** (schedules, webhooks) need `SubAgent.allow_unattended`.

## Management commands

- `run_due_triggers`: fire due schedules once (for when Celery isn't running).
- `recover_runs`: clean up runs orphaned by a restart.
- `install_packs`: install template packs into an account.

## Tests

`agents/tests/`. Good starting points: `test_agent_runtime.py`,
`test_autonomy.py`, `test_schedules.py`, `test_gallery.py`, and
`test_regressions.py` (grouped by the kind of mistake, worth reading).
