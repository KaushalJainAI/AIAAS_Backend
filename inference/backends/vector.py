"""
Vector backend — the original behaviour, wrapped.

The HNSW store in `inference.engine` is untouched; this adapter exists so the
rest of the system talks to *a* backend rather than to FAISS directly. Every
call is a delegation.
"""
from __future__ import annotations

from typing import List

from inference.engine import SearchResult, get_hnsw_kb

from .base import IngestResult, RetrievalBackend


class VectorBackend(RetrievalBackend):
    backend_name = 'vector'

    def _hnsw(self):
        return get_hnsw_kb(
            self.kb.id,
            getattr(self.kb, 's3_index_key', '') or f'indices/kb_{self.kb.id}',
        )

    async def ingest(self, document) -> IngestResult:
        hnsw = self._hnsw()
        await hnsw.initialize()
        chunk_ids = await hnsw.add_document(
            document.id,
            document.content_text or '',
            {'name': document.name, 'user_id': document.user_id, 'kb_id': self.kb.id},
        )
        return IngestResult(
            chunk_count=len(chunk_ids),
            status='indexed',
            detail=f'{len(chunk_ids)} vectors',
            extras={'ntotal': hnsw.ntotal, 'index_size_bytes': hnsw.index_size_bytes},
        )

    async def search(self, query, top_k=5, doc_id=None) -> List[SearchResult]:
        hnsw = self._hnsw()
        await hnsw.initialize()
        return await hnsw.search(query, top_k=top_k, doc_id=doc_id)

    async def remove_document(self, doc_id: int) -> bool:
        hnsw = self._hnsw()
        await hnsw.initialize()
        removed = await hnsw.delete_document(doc_id)
        return bool(removed)
