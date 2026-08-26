"""
Hybrid backend — vector and keyword search run together, ranks fused.

Neither retrieval style is strictly better. Semantic search answers "what is
this about"; keyword search answers "where exactly does this string appear" —
IDs, error codes, function names, the exact spelling of a name. A KB set to
hybrid gets both at ingest time (embeddings *and* the inverted index) and
fuses both result lists at query time.

Fusion is reciprocal rank fusion: each list contributes 1/(K + rank + 1) per
hit. RRF is used because it needs no score calibration — cosine similarity and
the keyword score live on incommensurable scales, but ranks are ranks.
"""
from __future__ import annotations

import asyncio
from typing import List, Tuple

from inference.engine import SearchResult

from .base import IngestResult, RetrievalBackend
from .fulltext import FullTextBackend
from .vector import VectorBackend

#: Standard RRF constant — larger flattens the head of each list, smaller
#: lets the top single hit dominate. 60 is the usual choice.
RRF_K = 60


def fuse(
    result_lists: List[List[SearchResult]],
    top_k: int,
) -> List[Tuple[str, float]]:
    """
    RRF over already-ranked lists. Returns [(dedupe_key, normalized_score)]
    sorted best first; callers map keys back to their results.
    """
    fused: dict = {}
    for results in result_lists:
        for rank, r in enumerate(results):
            key = f'{r.document_id}:{r.content[:100]}'
            contribution = 1.0 / (RRF_K + rank + 1)
            if key in fused:
                prev = fused[key]
                fused[key] = (prev[0] + contribution, prev[1])
            else:
                fused[key] = (contribution, r)
    # Normalize so the best possible fused score maps to ~1.0.
    ceiling = len(result_lists) * (1.0 / (RRF_K + 1))
    ranked = sorted(fused.items(), key=lambda kv: -kv[1][0])[:top_k]
    return [(key, s / ceiling) for key, (s, _r) in ranked]


class HybridBackend(RetrievalBackend):
    backend_name = 'hybrid'

    def __init__(self, kb):
        super().__init__(kb)
        self.vector = VectorBackend(kb)
        self.fulltext = FullTextBackend(kb)

    async def ingest(self, document) -> IngestResult:
        vector_result, fulltext_result = await asyncio.gather(
            self.vector.ingest(document),
            self.fulltext.ingest(document),
        )
        return IngestResult(
            chunk_count=max(vector_result.chunk_count, fulltext_result.chunk_count),
            status='indexed',
            detail=(
                f'semantic: {vector_result.detail}; keyword: {fulltext_result.detail}'
            ),
            # Both halves' extras ride up with the composed result. Dropping
            # them lost the vector half's `ntotal` / `index_size_bytes`, which
            # is the only thing `_sync_kb_stats` looks for — so a hybrid KB
            # reported 0 vectors and 0 bytes no matter how much it held.
            extras={**fulltext_result.extras, **vector_result.extras},
        )

    async def search(self, query, top_k=5, doc_id=None) -> List[SearchResult]:
        vector_results, fulltext_results = await asyncio.gather(
            self.vector.search(query, top_k=top_k, doc_id=doc_id),
            self.fulltext.search(query, top_k=top_k, doc_id=doc_id),
        )

        merged = fuse([vector_results, fulltext_results], top_k)
        by_key = {}
        for r in vector_results:
            by_key[f'{r.document_id}:{r.content[:100]}'] = r
        for r in fulltext_results:
            by_key.setdefault(f'{r.document_id}:{r.content[:100]}', r)

        out = []
        for key, score in merged:
            r = by_key.get(key)
            if r is not None:
                out.append(SearchResult(
                    document_id=r.document_id,
                    chunk_id=r.chunk_id,
                    content=r.content,
                    score=round(min(score, 1.0), 4),
                    metadata={**r.metadata, 'match': 'hybrid'},
                ))
        return out

    async def remove_document(self, doc_id: int) -> bool:
        removed_v, removed_f = await asyncio.gather(
            self.vector.remove_document(doc_id),
            self.fulltext.remove_document(doc_id),
        )
        return removed_v or removed_f
