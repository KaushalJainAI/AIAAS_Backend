"""
Retrieval backends — what "ingest" and "search" mean for a knowledge base.

A KnowledgeBase row used to hard-wire one mechanism end-to-end: chunk, embed,
FAISS HNSW. Supporting more kinds of retrieval does not mean a second copy of
the pipeline per kind; it means the pipeline branches behind one interface.

Each backend answers three questions for the KB it is attached to:

  ingest          what happens to a document's text (may be almost nothing)
  search          how a query becomes ranked results ([] when meaningless)
  remove_document what must be cleaned up when a document goes away

Results share the engine's `SearchResult` shape so every consumer above the
backends — chat tools, views, RAGPipeline — stays shape-stable no matter which
machinery produced the hit.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import List

from inference.engine import SearchResult


@dataclass
class IngestResult:
    """What one ingest pass did. `status` maps straight onto Document.status."""
    chunk_count: int = 0
    #: 'indexed' — retrievable by search; 'stored' — retrievable by reading.
    status: str = 'indexed'
    detail: str = ''
    extras: dict = field(default_factory=dict)


class RetrievalBackend(ABC):
    """
    One retrieval mechanism bound to one KnowledgeBase row.

    Implementations receive the ORM instance and do their own ORM work in
    sync context (`sync_to_async` at the call site or inside); anything
    engine-backed goes through the existing async engine API.
    """

    #: Matches KnowledgeBase.BACKEND_* values.
    backend_name: str = ''

    def __init__(self, kb):
        self.kb = kb

    @abstractmethod
    async def ingest(self, document) -> IngestResult:
        """Process `document.content_text` into whatever this backend searches."""

    @abstractmethod
    async def search(
        self,
        query: str,
        top_k: int = 5,
        doc_id: int | None = None,
    ) -> List[SearchResult]:
        """Rank results for `query`. Backends where search is meaningless
        (raw) return [] — callers point the agent at browse/read tools."""

    @abstractmethod
    async def remove_document(self, doc_id: int) -> bool:
        """Drop everything this backend kept for `doc_id`. False if none."""
