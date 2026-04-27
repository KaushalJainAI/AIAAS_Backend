# AIAAS Backend Chat Agent

This document explains the architecture and capabilities of the Standalone AI Chat Agent located within the `Backend/chat` module. This module powers the platform's "Perplexity-style", conversational AI interface.

## 1. Overview and Purpose

While the `KingOrchestrator` (`Backend/orchestrator/`) manages complex, deterministic multi-step DAG workflows, the **Chat Agent** provides a standalone, unstructured chat interface. 

It acts as an intelligent sidekick or "Copilot", designed to quickly answer questions, perform web research, suggest workflows, execute code, and summarize dragged-and-dropped documents without requiring the user to build a node-based workflow.

## 2. Core Capabilities

### A. Dynamic LLM Routing
The Chat Agent does not hardcode its LLM API calls. Instead, it leverages the `PROVIDER_NODE_MAP` and existing `Credential` schemas to route requests through the visual node registry.
- Supported Providers: `openai`, `gemini`, `ollama`, `openrouter`, `perplexity`, `huggingface`, `anthropic`, `deepseek`, `xai`.
- It dynamically fetches the active `Credential` for the requesting user based on the selected provider slug.

### B. Smart Memory & Context Management (Two-Tiered)
The Agent utilizes a sophisticated two-tiered context management system to optimize token usage while maintaining long-term memory.
- **Immediate Context (Flash Memory)**: During the initial turn a resource (Document/Web Page) is introduced, the LLM receives the **full extracted text** for high-fidelity analysis.
- **Historical Context (Summarized Memory)**: On subsequent turns, the system automatically truncates full resources into **Reference-ID backed Summaries**. 
  - For files: A ~1500 char extractive preview + UUID.
  - For web: Semantic snippets + Title/URL.
- Uses a **100,000 Maximum Context Token** safety limit to prevent context window overflow.
- A rolling **History Window of 50 messages** to manage data efficiently.
- Token tracking ensures LLM usage analytics are persistently stored on the session level (`total_tokens_used`).

### C. Intent-Aware Execution & Eager Tool Calls
Responses are categorized by "Intent" (e.g., `chat`, `search`, `image`, `video`, `workflow`, `coding`). Intents are passed explicitly by the frontend or classified via heuristics or slash commands.
- **Eager Tool Execution**: If the intent is explicitly `search`, `research`, or `workflow`, the chat agent directly invokes tools *before* the main LLM call. This saves an entire LLM "Agentic" planning roundtrip.
- **Slash Commands**: 
  - `/search <query>`: Triggers web search.
  - `/research <query>`: Triggers deep research loop.
  - `/image <query>`: Triggers image search/generation.
  - `/video <query>`: Triggers video search (coming soon).
  - `/workflow <query>`: Suggests a platform workflow.
  - `/coding <query>`: Activates Coding Mode with sandbox access.

### D. Coding Mode & Python Sandbox
Activated via `/coding` or predicted intent, this mode enables technical problem-solving.
- **`execute_python_code` Tool**: Allows the AI to write and run Python scripts.
- **Execution Engines**:
  - `in_process`: Fast execution with AST-based security limits.
  - `wasm`: Strict CPU/RAM isolation via WebAssembly for untrusted logic.
- **Safety**: Code is executed in a secure sandbox, capturing `stdout` and `stderr` for the AI to debug its own output.

### E. Integrated Web Search & Deep Research
Powered by DuckDuckGo (`perform_web_search`), the AI has live internet access. 
- **Standard Search**: Performs lookup and injects raw search snippet JSONs into the LLM prompt. Retains URLs as structured `sources` metadata.
- **Deep Research Loop**: When the intent is `research`, the agent shifts into an iterative data-gathering loop:
  1. A "Research Planner" LLM generates 2-4 distinct search queries and a link depth (15-50).
  2. The backend executes all searches and scrapes up to 60k text characters across all valid sources.
  3. The extracted texts are combined and embedded directly into the final LLM synthesis prompt.

### F. Document Indexing (Inference-style RAG)
Users can upload files natively. This follows the "Inference App" pattern.
- Support for PDFs, PPTX, TXT, CSV, JSON, and Images.
- **Autonomous Recall**: The system injects only a summary and ID. The AI uses the `read_attachment_text` tool to "fetch" full content if needed.

### G. Semantic Workflow Suggestion
- Utilizing `suggest_workflow`, it searches workflow descriptions, titles, and IDs.
- Returns a `workflow_suggestion` message payload allowing the frontend to present a clickable link.

### H. Extended Native Tools
- `web_search`: DuckDuckGo text/image/video search.
- `read_attachment_text`: Query DB for full file content.
- `read_url`: Scrape and convert a web page to text.
- `execute_python_code`: Secure code execution.
- `get_current_time`: Host system clock awareness.
- `get_chat_message_full_text`: Retrieve full content of summarized history messages.
- `call_internal_api`: Generic caller to simulate and execute internal backend Django REST APIs securely on the user's behalf.
- `dispatch_ui_actions`: Send real-time commands via WebSocket to the user's frontend to navigate, show toasts, or manipulate the visual ReactFlow canvas.

### I. Agentic Tool Loop & Stabilization
The chat engine features a robust iterative loop (`send_message_stream`) that allows the AI to use multiple tools in sequence.
- **Iteration Limits**: Dynamically bounded based on intent (e.g., higher for research).
- **Timeouts**: LLM calls (180s) and Tool runs (120s) have strict timeouts to prevent hanging.
- **JSON Repair**: If the LLM returns invalid JSON or fails to follow the schema, a "Repair" pass is triggered to normalize the output.
- **Sanitization**: Tool arguments are stripped of hallucinated XML/HTML tags before execution.

### J. Strict JSON Output Structure
The LLM must respond using a rigid JSON schema:
```json
{
  "response": "Detailed markdown explanation...",
  "summary": "Quick one-sentence summary...",
  "follow_ups": ["Q1", "Q2", "Q3"],
  "sources": [...],
  "thinking": "Internal reasoning process..."
}
```

### K. Context Referencing (Message Referencing)
Users can reference specific messages or highlighted text. This is injected as a silent system instruction to bias the LLM's attention without duplicating text in the prompt.

## 3. Database Architecture (`models.py`)

- **`ChatSession`**: Stores LLM configuration, session title, and token usage.
- **`ChatMessage`**: Stores turns with roles (`user`, `assistant`, `system`) and a `metadata` blob for citations, tool traces, and media.
- **`ChatAttachment`**: Stores uploaded files and extracted text. Linked to `inference.Document` for RAG support.

## 4. API Structure (`urls.py` / `views.py`)
- `POST /api/chat/sessions/`: Create session.
- `POST /api/chat/sessions/<id>/message/`: Sync/Async message endpoint.
- `GET /api/chat/sessions/<id>/stream/`: Server-Sent Events (SSE) stream for real-time AI response rendering.
- `POST /api/chat/sessions/<id>/upload/`: File upload.

## 5. Memory & Context Strategy
- **Two-Tiered Context**: Snippets for history, full text on-demand via tools.
- **Hierarchical RAG**: Scopes include File-level, User-level, and Platform-level knowledge.
- **Token Optimization**: Automatic truncation of older history and large tool blocks to stay within 100k token limits.
