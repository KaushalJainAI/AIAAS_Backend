"""
Raw backend — store documents; retrieval is reading, not searching.

For contracts, short references, anything the agent should see whole rather
than as decontextualized fragments. Ingest extracts text and stops — no
chunking, no embedding, no index. Search is deliberately meaningless here:
returning [] is the honest answer, and the tools' descriptions route the
agent to list_documents + read_document instead.
"""
from __future__ import annotations

from typing import List

from inference.engine import SearchResult

from .base import IngestResult, RetrievalBackend


class RawBackend(RetrievalBackend):
    backend_name = 'raw'

    async def ingest(self, document) -> IngestResult:
        text = document.content_text or ''
        return IngestResult(
            chunk_count=0,
            status='stored',
            detail=(
                f'Stored whole ({len(text):,} chars of extracted text) — '
                f'retrievable via list_documents + read_document'
            ),
        )

    async def search(self, query, top_k=5, doc_id=None) -> List[SearchResult]:
        return []

    async def remove_document(self, doc_id: int) -> bool:
        # Nothing was indexed; the Document row itself carries everything.
        return False
