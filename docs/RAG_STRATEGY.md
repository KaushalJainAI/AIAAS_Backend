# Hierarchical RAG Strategy in AIAAS

AIAAS employs a multi-level Retrieval-Augmented Generation (RAG) system designed to provide pinpoint context retrieval from documents of varying sizes while maintaining low latency and high relevance.

## Retrieval Backends (2026-08-24)

A `KnowledgeBase` is a container; its `backend` field picks the machinery that
ingestion and search mean for it. The backends live in `inference/backends/`
behind one interface (`ingest` / `search` / `remove_document`), so consumers
above them — chat tools, views, the extraction engine — never learn which one
ran.

| Backend | Ingestion | Search | Choose when |
|---------|-----------|--------|-------------|
| `vector` | chunk + embed → FAISS HNSW (the original behaviour) | semantic cosine similarity | prose, "what is this about" questions |
| `fulltext` | chunk only, no embeddings; terms go into an inverted index (`IndexedTerm`) | exact + prefix keyword matching, quoted phrases, BM25-flavoured ranking | IDs, invoice numbers, code identifiers, exact names |
| `raw` | extract text, store whole — nothing indexed (`Document.status='stored'`) | none: agent browses with `list_documents`, reads with `read_document` | contracts, short references, anything better read whole than as fragments |
| `hybrid` | both vector *and* fulltext indexes maintained | both searched in parallel, merged by reciprocal-rank fusion | corpora where meaning and exact strings both matter |

Design decisions worth knowing:

- **The keyword index is ours, not Postgres FTS / SQLite FTS5.** Dev runs
  SQLite and prod runs PostgreSQL whose full-text features share nothing. A
  posting table behaves identically on both and keeps matching semantics
  local: case-insensitive whole terms, prefix expansion for terms ≥ 3 chars
  (bounded, postings scan capped at 20k rows), expanded matches weighted at
  half strength so a literal hit always outranks a "starts with".
- **Ranking leads with distinct-terms-matched**, then an idf-weighted tf sum.
  Quoted phrases are verified verbatim against chunk text after ranking
  (positions are not stored); an all-phrases query falls back to a substring
  scan over the KB's chunks.
- **Misrouting is advice, not error.** The chat tools let the model choose the
  strategy: `knowledge_base_search` (semantic), `keyword_search` (exact),
  `list_documents` → `read_document` (raw). Point semantic search at a raw KB
  and it is told how to read instead of failing; point keyword search at a
  vector KB and it is told which tool works there.
- **Backend changes are locked once documents exist** — switching machinery
  under ingested content would orphan vectors or postings. `KnowledgeBase` is
  internal (no HTTP CRUD) — one implicit Default per user; empty KBs may switch
  backend only via internal model update, not via an endpoint.
- **Deletion fans out through every backend** (`tasks.remove_document_from_kb`),
  so no backend can leak state when a document goes away.

## Three-Tiered Architecture

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

## Technical Flow

1.  **Ingestion**:
    *   Files are uploaded via `/chat/sessions/<id>/upload/`.
    *   Text is extracted and an `inference.Document` is created.
    *   A background thread (`inference.tasks.process_document`) runs the target KB's ingest backends: vector/hybrid chunk + embed into FAISS, fulltext chunks and builds the inverted index, raw stores the text alone (see "Retrieval Backends" above).
    *   The extraction engine (`inference/extraction.py`) shares this ingestion lifecycle: it reads the same `Document` rows (text or, for image/video, the file itself as an attachment) and one LLM call per document fills a schema.
2.  **Retrieval**:
    *   On every message, `send_message_stream` performs a parallel search across all three tiers.
    *   Targeted search is prioritized if a large file is present in the current session.
3.  **Context Injection**:
    *   Top results are formatted under the header `### RELEVANT CONTEXT FROM DOCUMENTS`.
    *   Total context is capped at 10 high-quality snippets.

## AI Directives
The system message includes a specific directive (Directive 9) instructing the LLM:
> "RAG CONTEXT UTILIZATION: If provided with context labeled 'RELEVANT CONTEXT FROM DOCUMENTS', prioritize these snippets. These are pinpointed chunks from documents too large for manual extraction or direct input; they provide the highest fidelity for detailed queries about large files."

## File Support
- **Multimodal Models**: Gemini and Anthropic models receive small files directly via base64 for native interpretation.
- **Standard Models**: Fall back to RAG context for all supported file types (PDF, PPTX, TXT, MD, CSV, JSON).
