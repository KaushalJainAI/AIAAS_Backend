# Nodes App Documentation

This document provides a comprehensive overview of the nodes system, including all available nodes, their configuration, and issue tracking.

---

## 📁 Directory Structure

```
nodes/
├── __init__.py
├── admin.py              # Django admin registration
├── apps.py               # App config with lazy node registration
├── models.py             # CustomNode model for user-created nodes
├── tests.py              # Unit tests
├── urls.py               # API endpoints
├── views.py              # REST API views
├── handlers/
│   ├── __init__.py
│   ├── base.py           # BaseNodeHandler, Pydantic models, helper methods
│   ├── registry.py       # NodeRegistry singleton with lazy loading
│   ├── core_nodes.py     # Code, Set, If
│   ├── llm_nodes.py      # OpenAI, Gemini, Ollama, Perplexity, OpenRouter
│   ├── integration_nodes.py  # Gmail, Slack, Sheets, Discord, Notion, HTTPRequest, etc.
│   ├── triggers.py       # All trigger nodes (13 types)
│   ├── logic_nodes.py    # Loop, SplitInBatches (with full loop support)
│   ├── subworkflow_node.py   # Execute nested workflows
│   ├── langchain_nodes.py    # LangChain tool wrapper
│   └── custom_loader.py      # Dynamic custom node loader
```

---

## 🔁 Loop Support

The compiler now fully supports loops via `LoopNode` and `SplitInBatchesNode`.

### How Loops Work

```
Trigger → LoopNode → [loop] → BodyNode → (back-edge) → LoopNode
                  ↘ [done] → NextNode
```

1. **DAG Validator** allows cycles when they contain a loop node
2. **Compiler** tracks `loop_stats` and increments iteration counter
3. **Loop nodes** return `loop` or `done` handle based on current iteration
4. **Results are accumulated** from loop body nodes for retrieval when loop completes

### LoopNode Configuration

| Field | Type | Description |
|-------|------|-------------|
| `max_loop_count` | NUMBER | Maximum iterations (safety limit, default: 10) |
| `items_field` | STRING | Optional field name with array to iterate over |

**Outputs:**
- `loop` → Current item/iteration index passed to loop body
- `done` → Accumulated results from all iterations

### SplitInBatchesNode Configuration

| Field | Type | Description |
|-------|------|-------------|
| `batch_size` | NUMBER | Items per batch (default: 1) |
| `max_loop_count` | NUMBER | Maximum batches (safety limit, default: 100) |
| `items_field` | STRING | Optional field name with array to split |

**Outputs:**
- `loop` → Current batch with `batch`, `batch_index`, `total_batches`, `is_last_batch`
- `done` → Accumulated results from all batches

### ExecutionContext Loop Helpers

```python
context.get_loop_count(node_id)       # Current iteration count
context.increment_loop(node_id)        # Increment and return count
context.get_batch_cursor(node_id)      # Current cursor position
context.set_batch_cursor(node_id, pos) # Update cursor
context.get_loop_items(node_id)        # Get items being iterated
context.set_loop_items(node_id, items) # Store items
context.accumulate_loop_result(node_id, result)  # Add to results
context.get_accumulated_results(node_id)         # Get all results
```

---

## 📦 Registered Nodes (37 Total)

### Trigger Nodes (13)

| Node Type | Name | Description |
|-----------|------|-------------|
| `manual_trigger` | Manual Trigger | Start on user action |
| `webhook_trigger` | Webhook Trigger | Start on HTTP request |
| `schedule_trigger` | Schedule Trigger | Start on cron schedule |
| `email_trigger` | Email Trigger | Start on email received |
| `form_trigger` | Form Trigger | Start on form submission |
| `slack_trigger` | Slack Trigger | Start on Slack event |
| `google_sheets_trigger` | Google Sheets Trigger | Start on sheet change |
| `github_trigger` | GitHub Trigger | Start on repo event |
| `discord_trigger` | Discord Trigger | Start on Discord message |
| `telegram_trigger` | Telegram Trigger | Start on Telegram message |
| `rss_feed_trigger` | RSS Feed Trigger | Start on new RSS item |
| `file_trigger` | File Trigger | Start on file change |
| `sqs_trigger` | SQS Trigger | Start on AWS SQS message |

### Core Nodes (3)

| Node Type | Name | Description |
|-----------|------|-------------|
| `code` | Code | Execute custom Python code |
| `set` | Set | Set/transform data fields |
| `if` | If | Conditional branching |

### LLM Nodes (5)

| Node Type | Name | Description |
|-----------|------|-------------|
| `openai` | OpenAI | GPT-4o, GPT-4, GPT-3.5 |
| `gemini` | Gemini | Gemini 2.0, 1.5 Flash/Pro |
| `ollama` | Ollama (Local) | Local LLM via Ollama |
| `perplexity` | Perplexity | Web-grounded AI search |
| `openrouter` | OpenRouter | Unified multi-LLM gateway |

### Integration Nodes (11)

| Node Type | Name | Description |
|-----------|------|-------------|
| `http_request` | HTTP Request | Make custom HTTP/API requests |
| `gmail` | Gmail | Send emails via Gmail API |
| `slack` | Slack | Send Slack messages |
| `google_sheets` | Google Sheets | Read/write spreadsheets |
| `discord` | Discord | Send Discord messages |
| `notion` | Notion | Manage Notion pages/databases |
| `airtable` | Airtable | CRUD on Airtable records |
| `telegram` | Telegram | Send Telegram messages |
| `trello` | Trello | Manage Trello cards/boards |
| `github` | GitHub | GitHub API operations |

### Logic Nodes (2)

| Node Type | Name | Description |
|-----------|------|-------------|
| `loop` | Loop | Iterate over items or by count |
| `split_in_batches` | Split In Batches | Process arrays in batches |

### Special Nodes (3)

| Node Type | Name | Description |
|-----------|------|-------------|
| `subworkflow` | Execute Workflow | Run nested workflows |
| `mcp_tool` | MCP Tool | Execute MCP server tools |
| `langchain_tool` | LangChain Tool | Run LangChain tools |

---

## 🔧 NodeRegistry

Singleton pattern with lazy loading:

```python
from nodes.handlers.registry import get_registry

registry = get_registry()  # Triggers lazy registration
handler = registry.get_handler('loop')
schemas = registry.get_all_schemas()
```

---

## ✅ All Issues Fixed

| Issue | Status |
|-------|--------|
| Duplicate node registration | ✅ Fixed |
| HTTPRequestNode defined twice | ✅ Fixed |
| LangChainToolNode return type | ✅ Fixed |
| Missing error handling | ✅ Fixed |
| `_create_child_context` not implemented | ✅ Fixed |
| Hardcoded timeout | ✅ Fixed |
| Missing TYPE_CHECKING guard | ✅ Fixed |
| Unused imports | ✅ Fixed |
| Missing NodeExecutionResult import | ✅ Fixed |
| **Loop support not working** | ✅ Fixed |

---

## 📊 Summary

| Category | Count |
|----------|-------|
| Trigger Nodes | 13 |
| Core Nodes | 3 |
| LLM Nodes | 5 |
| Integration Nodes | 11 |
| Logic Nodes | 2 |
| Special Nodes | 3 |
| **Total** | **37** |
