# Hierarchical RAG Strategy in AIAAS

AIAAS employs a multi-level Retrieval-Augmented Generation (RAG) system designed to provide pinpoint context retrieval from documents of varying sizes while maintaining low latency and high relevance.

## 🏗 Three-Tiered Architecture

### 1. File Level (Targeted Retrieval)
- **Scope**: Specific to a single document.
- **Trigger**: Automatically enabled for documents exceeding **30,000 characters**.
- **Purpose**: When a user asks a highly detailed question about a specific large file, the system bypasses general knowledge and searches only that file's specific vector index.
- **Mapping**: Managed via `ChatAttachment` link to `inference.Document`.

### 2. User Level (Library Retrieval)
- **Scope**: All documents uploaded by a specific user across all sessions.
- **Trigger**: Every document upload (Text, PDF, PPTX) is indexed here.
- **Purpose**: Provides cross-document context. Allows the AI to connect dots between multiple smaller files in the user's private library.

### 3. Platform Level (Shared Retrieval)
- **Scope**: A global knowledge base of shared knowledge.
- **Trigger**: Documents marked as `shared` via the Inference API.
- **Purpose**: Provides institutional or community knowledge that transcends individual user sessions.

## 🚀 Technical Flow

1.  **Ingestion**:
    *   Files are uploaded via `/chat/sessions/<id>/upload/`.
    *   Text is extracted and an `inference.Document` is created.
    *   A background thread (`inference.tasks.process_document`) chunks the text and generates embeddings using FAISS.
2.  **Retrieval**:
    *   On every message, `send_message_stream` performs a parallel search across all three tiers.
    *   Targeted search is prioritized if a large file is present in the current session.
3.  **Context Injection**:
    *   Top results are formatted under the header `### RELEVANT CONTEXT FROM DOCUMENTS`.
    *   Total context is capped at 10 high-quality snippets.

## 🤖 AI Directives
The system message includes a specific directive (Directive 9) instructing the LLM:
> "RAG CONTEXT UTILIZATION: If provided with context labeled 'RELEVANT CONTEXT FROM DOCUMENTS', prioritize these snippets. These are pinpointed chunks from documents too large for manual extraction or direct input; they provide the highest fidelity for detailed queries about large files."

## 📄 File Support
- **Multimodal Models**: Gemini and Anthropic models receive small files directly via base64 for native interpretation.
- **Standard Models**: Fall back to RAG context for all supported file types (PDF, PPTX, TXT, MD, CSV, JSON).
