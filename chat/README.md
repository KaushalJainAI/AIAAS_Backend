# `chat/`: the assistant, the AI loop, and every tool

The biggest app (~30k lines). It holds three different things:

1. **The chat feature**: sessions, messages, attachments.
2. **The AI loop** (`turn/`), which saved agents also use.
3. **The tool library** (`tools/`): everything the AI can do, for chat and agents.

Full design: [`docs/CHAT_AGENT.md`](../docs/CHAT_AGENT.md).

## Read in this order

1. `tools/clock.py`: the smallest tool. Shows what a tool looks like.
2. `tools/registry.py`: what `@tool(...)` records (schema, `effect`, `parallel`, `sensitive`, `requires`).
3. `turn/pipeline.py` → `run_chat_turn`: one message, end to end.
4. `turn/agent.py` → `_build_graph`, `run_turn`, `TurnContext`: the loop.
5. `tools/permissions.py`: which calls need your approval.

## Data (`models.py`)

| Model | What it is |
|---|---|
| `ChatSession` | One conversation. Holds its model, effort level, autonomy mode, memory on/off |
| `ChatMessage` | One message (yours or the assistant's), with sources, trace and cost in `metadata` |
| `ChatAttachment` | An uploaded file on a session |
| `ToolOutput` | A tool result too big to show the model, stored so `read_tool_output` can fetch it |
| `ToolPermission` | "Always allow" / "allow this session" answers to approval prompts |
| `VisionExchange` | Questions asked about an image, and the answers |

## Folders

| Folder | What is in it |
|---|---|
| `turn/` | Running one turn: `pipeline` (the steps), `agent` (the loop), `prompts`, `history`, `curation` (shrink long runs), `steering` (messages mid-run), `todos` (the run's plan), `reviewer` (auto-mode judge), `checkpoints` (where run state is saved), `runs` (turns that outlive the request), `events` (what is streamed) |
| `tools/` | One file per topic. See the list below |
| `commands/` | Slash commands. `registry.py` declares them, `resolve.py` parses them, one file per group |
| `transport/` | Streaming the events over HTTP |
| `sources/` | Web search (`search.py`) and file attachments (`attachments.py`) |
| `vision/` | A cheap vision model a text-only model can question about an image |
| `guest/` | Chat for visitors without an account |

## The tool files

| File | Tools |
|---|---|
| `web.py`, `fetch.py`, `browser.py` | Web search, reading pages, downloading files, driving a browser |
| `knowledge.py` | Searching your knowledge bases |
| `files.py` | Reading and writing your files (`inference/vfs.py`): versions, restore, export, block/slide edits |
| `sandbox.py` | Running Python (`execute_python`, `run_python_on_files`) |
| `office/` | Making `.pptx`, `.xlsx`, `.docx`, `.pdf` and diagrams from a description; reading and editing workbooks (`read_workbook`, `edit_workbook`) |
| `charts.py`, `artifacts.py`, `dashboards.py`, `publish.py` | Drawing charts, HTML snippets, dashboards, shareable pages |
| `google/`, `notion.py` | Gmail, Drive, Sheets, Calendar, Docs, Notion |
| `messaging/`, `talk.py` | Slack, WhatsApp, Teams, SMS, Telegram |
| `data.py`, `apicaller.py` | Your databases and your own APIs |
| `agents.py`, `authoring.py`, `tasks.py` | Running agents, creating agents by description, the coding team's task dispatch |
| `memory.py`, `conversation.py`, `planning.py` | What the assistant remembers about you, searching this chat, the run's todo list |
| `media.py`, `voice.py`, `vision.py`, `docs.py` | Images, speech, questions about an image, reading scanned documents |
| `code.py`, `compute.py` | Coding and compute tools. Hidden until a workspace engine exists |
| `workspace.py`, `runs.py`, `missions.py`, `esign.py`, `ask.py`, `eval_manager.py`, `internal.py`, `clock.py` | Smaller single-purpose tools |
| `permissions.py` | Decides which call needs approval |
| `describe.py` | Turns a tool call into a sentence a person can read on an approval card |
| `tool_output.py` | Caps huge tool results and stores the rest |
| `__init__.py` | Imports every tool file (that is what registers them) and picks which tools a turn is offered. Chat gets only reads plus delegation, agent-building, memory and missions (`chat_orchestrator_allowed`); chat's calls go through `execute_chat_tool`, agents' through `execute_tool` |

## How to add a tool

See "Common jobs" in [`START_HERE.md`](../../START_HERE.md#9-common-jobs-step-by-step).
In short: `@tool({...})` on an `async def f(args, context) -> str`, import the
file in `tools/__init__.py`, and add the name to `GRANT_TOOLS` in
`agents/agent/runtime.py` if agents should get it. Chat only gets tools with
`effect="read"`: anything that writes, sends or spends is an agent's job.

## Watch out for

- **Tools must check ownership themselves.** `/api/chat/execute-tool/` can call
  any tool with any arguments.
- **Declare `effect` honestly.** It defaults to `"irreversible"`, the safest
  guess. `auto` mode uses it to decide what needs approval.
- **Don't put changing things in the system prompt** (see `turn/prompts.py`).
  Put them in the context update message instead, or the provider's prompt
  cache stops working.
- **Tests that fake the model patch `llm.access.stream`.**
