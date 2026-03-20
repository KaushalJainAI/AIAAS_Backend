# AIAAS Backend Chat Agent

This document explains the architecture and capabilities of the Standalone AI Chat Agent located within the `Backend/chat` module. This module powers the platform's "Perplexity-style", conversational AI interface.

## 1. Overview and Purpose

While the `KingOrchestrator` (`Backend/orchestrator/`) manages complex, deterministic multi-step DAG workflows, the **Chat Agent** provides a standalone, unstructured chat interface. 

It acts as an intelligent sidekick or "Copilot", designed to quickly answer questions, perform web research, suggest workflows, and summarize dragged-and-dropped documents without requiring the user to build a node-based workflow.

## 2. Core Capabilities

### A. Dynamic LLM Routing
The Chat Agent does not hardcode its LLM API calls. Instead, it leverages the `PROVIDER_NODE_MAP` and existing `Credential` schemas to route requests through the visual node registry.
- Supported Providers: `openai`, `gemini`, `ollama`, `openrouter`, `perplexity`, `huggingface`, `anthropic`, `deepseek`.
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
Responses are categorized by "Intent" (e.g., `chat`, `search`, `image`, `video`, `workflow`). Intents are passed explicitly by the frontend or classified via heuristics (like `what is...`, `how to...` prompts).
- **Eager Tool Execution**: If the intent is explicitly `search` or `workflow`, the chat agent directly invokes the web search or workflow lookup *before* communicating with the LLM. This saves an entire LLM "Agentic" planning roundtrip, reducing response times by ~5–10 seconds.
- It then injects the tool outputs into the LLM context to synthesize the final markdown response.

### D. Integrated Web Search & Deep Research
Powered by DuckDuckGo (`perform_web_search`), the AI has live internet access. 
- **Standard Search**: Performs lookup and injects raw search snippet JSONs into the LLM prompt. Retains URLs as structured `sources` metadata.
- **Deep Research Loop**: When the intent is `research` (e.g., via `/research` command), the agent shifts into an iterative data-gathering loop:
  1. A standalone LLM runs the user's prompt alongside the exact system Datetime/Location strings.
  2. The LLM generates a comprehensive JSON execution plan defining 2-4 nuanced web search queries and a decided cap of links to consume (15-50).
  3. The backend executes all searches consecutively to gather unique URLs.
  4. An asynchronous web scraper natively visits each URL and leverages BeautifulSoup to ingest up to 60k text characters of live content.
  5. The extracted texts are combined and embedded directly into the final LLM synthesis prompt.
- **Session Locking**: Due to the heavily contextual nature of full deep research, if a chat session is initialized with the `research` intent, the UI natively *locks* the session into Deep Research mode preventing accidental mid-stream mode switching.

### E. Document Indexing (Inference-style RAG)
Users can upload files natively via the `/chat/sessions/{id}/upload/` endpoint. This follows the "Inference App" pattern where documents are treated as persistent, indexed resources.
- Support for PDFs, PPTX, TXT, CSV, JSON, and Images.
- Native parser functions (`_extract_pdf_text_sync`, etc.) extract text and securely save it in the database as a `ChatAttachment` with a unique **Reference ID (UUID)**.
- **Autonomous Recall**: Instead of bloating the context by injecting the full file on every turn, the system injects only a summary and the Reference ID. The AI is explicitly instructed that it has the decision-making power to use the `read_attachment_text` tool to "fetch" the full content from the database if the summary is insufficient.

### F. Semantic Workflow Suggestion
The Agent indexes the user's available visual workflows in the `orchestrator_workflow` table.
- Utilizing `suggest_workflow`, it searches workflow descriptions, titles, and IDs.
- If it detects the user wants to accomplish a structured task (e.g. "Send an email"), it returns a custom `workflow_suggestion` message payload allowing the frontend to quickly present a clickable link to that workflow.

### G. Extended Native Tools
Beyond search and workflow lookup, the Chat Agent natively includes utility tools:
- **`read_attachment_text`**: Allows the AI to query the database by Reference ID to retrieve the full context of a previously uploaded document. This is the core of the autonomous recall capability.
- **`read_url`**: Directly scrapes and converts a web page completely into raw text using BeautifulSoup. Injectable automatically into the LLM flow without writing any python.
- **`get_current_time`**: Exposes the host's system clock for native timestamp awareness. Additionally, the system automatically injects the exact Datetime via `system_prompt` on every conversational turn to inherently ground the AI.

### H. Strict JSON Structure Enforcements
To ensure a consistent UI experience, the LLM is explicitly instructed to respond using a rigid JSON schema:
```json
{
  "response": "Detailed markdown explanation...",
  "follow_ups": ["Can you clarify X?", "What is Y?", "How do I do Z?"]
}
```
A robust fallback parser uses regex and substring matching to extract the JSON safely even if the LLM hallucinates markdown code blocks or additional conversational text.

### I. Context Referencing (Message Referencing)
Users can highlight text in the UI and seamlessly reference it without copying raw text bounds. The frontend sends `reference: {message_id: int}` directly to the chat backend API, which is silently injected as a `[SYSTEM INSTRUCTION]` to bias the LLM's attention to the specific portion of the selected conversational history. This feature drastically reduces token replication overhead while maintaining high-fidelity conversational grounding.

## 3. Database Architecture (`models.py`)

- **`ChatSession`**: Defines a single interaction thread. Stores the LLM configuration (Model + Provider) and total token telemetry.
- **`ChatMessage`**: Represents turns in the conversation. Stores strict `role` enums (`user`, `assistant`, `system`) and utilizes a powerful `metadata` JSON blob to store citations, execution durations, workflow targets, and bounding variables.
- **`ChatAttachment`**: Stores uploaded media and previously extracted raw text to prevent repetitive document parsing over multiple message turn cycles.

## 4. API Structure (`urls.py` / `views.py`)
- `POST /api/chat/sessions/`: Create a new standalone chat session.
- `POST /api/chat/sessions/<id>/message/`: Main websocket-like response generator handling the heavy Agentic loop.
- `POST /api/chat/sessions/<id>/upload/`: Ingress endpoint for conversational attachments.
- `DELETE /api/chat/sessions/<id>/message/<id>/`: Handles targeted message expunging, automatically cleaning up underlying attachments from the storage volume.
## Advanced Interaction Patterns

### 🚀 Eager Intent Execution
Standard agent loops wait for the LLM to call tools. The Standalone Chat Agent uses a hybrid "Eager" approach for high-confidence intents:
- **Search/Research/Workflow**: These trigger tools *before* the first LLM generation. 
- **Synthesis Prompting**: Tool results are injected into the initial system prompt as "Additional context from tools," leading to faster, more accurate first responses.

### 🔍 Deep Research Clarification Loop
The `/research` intent follows a rigorous multi-stage pipeline:
1. **Clarification Check**: An LLM analyzes the query. If too broad (e.g., "AI industry"), it pauses to ask the user a specific clarifying question.
2. **Strategy Planning**: Once clear, a "Research Planner" LLM generates 2-4 distinct search queries and a target link depth (15-50 links).
3. **Execution**: Concurrent scraping and text extraction synthesized into a final report.

## 🧠 Memory & Context Strategy

### Two-Tiered Context Management
To maintain high performance and low token costs, the agent uses a differential context strategy:
- **Phase 1: Flash Summary**: Upon file upload, a ~1500 character snippet is immediately extracted and persisted in the conversation thread for "instant context."
- **Phase 2: Deep Knowledge (RAG)**: Files larger than **30,000 characters** are indexed into the Hierarchical RAG system. The RAG pipeline uses a **Global Singleton Embedder** (`all-MiniLM-L6-v2`) shared across all user sessions to ensure near-zero latency and minimal server memory footprint.
- **Phase 3: Deep Recall**: The AI can fetch any part of the document on-demand using the `read_attachment_text` tool, referencing the stored `attachment_id`.

### Hierarchical RAG Integration
The agent automatically searches three scopes for every query:
1. **File Level**: Targeted retrieval if the user specifies a particular attachment.
2. **User Level**: Searches all documents previously uploaded by the session owner.
3. **Platform Level**: Searches global, shared knowledge bases.
