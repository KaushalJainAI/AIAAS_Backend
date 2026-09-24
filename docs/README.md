# Backend docs: what to read

There are about 40 files here. Most are **plans**: notes written before a
feature was built. They explain *why* things are the way they are, but some
describe designs that changed later. Read the **reference** docs first. They
describe how the code works today.

New to the codebase? Start with [`../../START_HERE.md`](../../START_HERE.md).

## Reference: how it works now

Read these in roughly this order.

| Doc | What it explains |
|---|---|
| [CHAT_AGENT.md](CHAT_AGENT.md) | The chat assistant: one message, end to end, and the AI loop |
| [API.md](API.md) | Every HTTP route: what it does, who may call it, which tables it touches. **Update it whenever you change a route.** |
| [AGENT_OBSERVABILITY.md](AGENT_OBSERVABILITY.md) | How a run is recorded: run → turns → steps, plus agent revisions |
| [CONTEXT_LIFECYCLE.md](CONTEXT_LIFECYCLE.md) | How a long run stays inside the model's memory limit |
| [SANDBOX_EXECUTION.md](SANDBOX_EXECUTION.md) | How the AI's Python code runs safely |
| [CREDENTIALS_AND_SECURITY.md](CREDENTIALS_AND_SECURITY.md) | How API keys are stored and used |
| [MCP_ARCHITECTURE.md](MCP_ARCHITECTURE.md) | How outside tool servers (MCP) are connected. [MCP_INTEGRATION.md](MCP_INTEGRATION.md) is a shorter overview |
| [RAG_STRATEGY.md](RAG_STRATEGY.md) | How knowledge-base search works |
| [EVALUATION.md](EVALUATION.md) | How agents are tested and scored |
| [NOTIFICATION_SERVICE.md](NOTIFICATION_SERVICE.md) | Notifications and approval reminders |
| [IMAGINE.md](IMAGINE.md) | Image / video / audio generation |
| [VISION_AGENT.md](VISION_AGENT.md) | How a text-only model gets answers about an image |
| [AGENT_TEMPLATES.md](AGENT_TEMPLATES.md) | Installable agent templates (§3's data model is out of date: `Workflow` became `SubAgent`) |
| [WEBHOOK_TRIGGERS.md](WEBHOOK_TRIGGERS.md) | How a webhook starts an agent run |
| [ENGINEERING_DECISIONS.md](ENGINEERING_DECISIONS.md) | The hardest problems and why each was solved the way it was |

## Plans that are built

Useful for the "why". The code is the source of truth where they differ.

| Doc | Status |
|---|---|
| [SPECIALIST_AGENTS_PLAN.md](SPECIALIST_AGENTS_PLAN.md) | Built 2026-09-20: office files, specialists as templates |
| [PLATFORM_CAPABILITIES_PLAN.md](PLATFORM_CAPABILITIES_PLAN.md) | P0–P10 built 2026-09-21: messaging, data, browser, missions, auto mode, slash commands |
| [CODING_AGENTS_PLAN.md](CODING_AGENTS_PLAN.md) | Built 2026-09-22, but needs a workspace engine to actually run |
| [EVALUATION_PRODUCTION_PLAN.md](EVALUATION_PRODUCTION_PLAN.md) | Built 2026-09-19; paid benchmark runs not done yet |
| [SCHEDULES_SIMPLIFICATION_PLAN.md](SCHEDULES_SIMPLIFICATION_PLAN.md) | Phases 1–4 built 2026-09-22 |
| [OPENCODE_ZEN_PLAN.md](OPENCODE_ZEN_PLAN.md) | Built 2026-09-22; not tested with a real key |
| [AGENT_CONFIG_IMPROVEMENT_PLAN.md](AGENT_CONFIG_IMPROVEMENT_PLAN.md) | Audit of agent settings, 2026-09-18 |
| [TOOL_CONFIGURATION_PLAN.md](TOOL_CONFIGURATION_PLAN.md) | The Tools page and per-user tool switches |
| [EXTRACTION_MERGE.md](EXTRACTION_MERGE.md) | Done 2026-08-18: `extraction` folded into `inference` |
| [POSTGRES_PRODUCTION_MIGRATION_PLAN.md](POSTGRES_PRODUCTION_MIGRATION_PLAN.md) | Moving production to PostgreSQL |
| [GAP_CLOSURE_PLAN.md](GAP_CLOSURE_PLAN.md) | Done 2026-09-24: gaps found in a codebase sweep (email default, pack availability, flaky tests, lint, Memory and Missions pages) |

## Plans in progress or not started

| Doc | Status |
|---|---|
| [CONCURRENCY_LAG_FIX_PLAN.md](CONCURRENCY_LAG_FIX_PLAN.md) | Phases 0–1 built 2026-09-24, the rest not |
| [CUSTOM_TOOLS_PLAN.md](CUSTOM_TOOLS_PLAN.md) | Approved, being built: user-made tools |
| [RUN_VISIBILITY_AND_REMINDERS_PLAN.md](RUN_VISIBILITY_AND_REMINDERS_PLAN.md) | Approved, being built |
| [EVAL_EXPANSION_PLAN.md](EVAL_EXPANSION_PLAN.md) | Eval datasets for every agent |
| [COMPUTE_ISOLATION_PLAN.md](COMPUTE_ISOLATION_PLAN.md) | For discussion: a private machine per user (needed by the coding agents) |
| [PRODUCTIVITY_SUITE_PLAN.md](PRODUCTIVITY_SUITE_PLAN.md) | Proposed: in-browser office apps |
| [ORCHESTRATOR_LATENCY_OPTIMIZATION_PLAN.md](ORCHESTRATOR_LATENCY_OPTIMIZATION_PLAN.md) | Ideas for faster responses; its "current state" numbers are estimates |

## History: kept for the record, do not follow

These describe the old drag-and-drop workflow product, which was removed.

| Doc | Why it is kept |
|---|---|
| [WORKFLOW_RETIREMENT.md](WORKFLOW_RETIREMENT.md) | The decision to remove the workflow editor (done 2026-08-17) |
| [AGENT_BLOCKS_PLAN.md](AGENT_BLOCKS_PLAN.md) | "Agents from blocks", the step between workflows and agents |
| [AGENT_WORKFLOW_MERGE_PLAN.md](AGENT_WORKFLOW_MERGE_PLAN.md) | Superseded 2026-08-14 |
| [AGENT_WORKFLOW_UNIFICATION.md](AGENT_WORKFLOW_UNIFICATION.md) | Never built |
| [tool_calling_architecture_review.md](tool_calling_architecture_review.md) | An early review; the tool registry has since been rewritten (`chat/tools/registry.py`) |
| [learnings/](learnings/) | Notes from past performance work |
