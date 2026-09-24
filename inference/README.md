# `inference/`: the user's files, folders and knowledge bases

Despite the name, this app is mostly about **your stuff**:

- **Files and folders**: a per-user file tree (the Documents page).
- **Recycle bin**: deleting moves a file to the bin; a sweep empties it later.
- **Knowledge bases (KBs)**: sets of documents indexed for search (RAG).
- **The agent's file system** (`vfs.py`): lets the AI use paths like
  `/reports/q1.md` over your file tree, safely.
- **Extraction**: pulling structured rows out of documents.
- **Hosted pages and dashboards**: shareable snapshots and live tiles.

"Folders organise, knowledge bases index." A document has a folder *and*
may be in a KB, independently. Moving a file between folders never re-indexes it.

## Read in this order

1. `models.py` → `Folder`, `Document`, `KnowledgeBase`.
2. `filesystem.py`: the only way a folder id from a request becomes a `Folder`.
3. `vfs.py`: the path-based view the AI uses.
4. `engine.py` and `backends/`: how KB search works.

## Data (`models.py`)

| Model | What it is |
|---|---|
| `Folder` | A folder. `folder=NULL` on a document means "at the root" (there is no root row) |
| `Document` | A file: its bytes, extracted text (`content_text`), folder, KB, `deleted_at` if in the bin |
| `KnowledgeBase` | A searchable set of documents. `backend` says how it is searched |
| `DocumentChunk`, `IndexedTerm` | Pieces of documents for vector search, and words for keyword search |
| `ExtractionSchema`, `ExtractedRow` | "Pull these fields out of these files", and the rows that came out |
| `PublishedPage` | A snapshot shared by link (`/p/<slug>`) |
| `Dashboard` | Live tiles on the Dashboards page |

Trashed rows are hidden automatically: the default manager (`LiveManager`)
filters them out, so ordinary queries never see the bin.

## Files

| File | What it does |
|---|---|
| `views.py`, `urls.py` | Documents, KBs and search over HTTP |
| `folder_views.py` | The folder tree and the recycle bin over HTTP |
| `filesystem.py` | Folder lookups and moves. Unknown and not-yours ids both give 404 |
| `vfs.py` | The AI's file system: list, read, write, edit, find, delete, inside a scope |
| `recycle.py` | Trash, restore, and the sweep that deletes for good |
| `engine.py` | The vector index (HNSW) behind semantic search |
| `backends/` | The four ways a KB can be searched: `vector`, `fulltext` (keywords), `hybrid` (both), `raw` (no search, just read) |
| `utils.py` | Reading text out of uploaded files (PDF, Word, Excel...) |
| `tasks.py` | Background jobs: turning an upload into chunks and embeddings |
| `reindex.py`, `migration_tasks.py` | Re-indexing after the embedding model changes |
| `signals.py` | Keeps each KB's document count right |
| `extraction.py`, `extraction_views.py`, `extraction_urls.py` | The extraction engine and its API (served at `/api/extraction/`) |
| `pages.py`, `page_views.py` | Publishing and serving hosted pages |
| `dashboard_views.py` | Dashboards |
| `office_edit.py` | Creating and editing files from the in-browser apps |

## Safety rules

- **The API never takes a path**, only ids. Paths are for display.
- **The AI's file system never touches the real disk.** `vfs.py` never imports
  `os`. The worst a bug can reach is another database row, never a server file.
- **Paths are walked one folder at a time**, never matched as strings.
- **Deleting goes to the recycle bin**, so an AI mistake can be undone.

Search design: [`docs/RAG_STRATEGY.md`](../docs/RAG_STRATEGY.md).

## Management commands

`purge_recycle_bin`, `reindex_all`, `run_extraction`.

## Tests

`inference/tests/`: `test_filesystem.py`, `test_vfs.py`, `test_recycle.py`,
`test_chat_files.py`, `test_file_types.py`.
