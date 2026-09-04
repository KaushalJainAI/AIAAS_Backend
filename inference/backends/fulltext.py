"""
Fulltext backend — grep-shaped retrieval over a DB inverted index.

Why not Postgres FTS or SQLite FTS5: this project develops on SQLite and
deploys on PostgreSQL, and the two engines' full-text features share nothing —
configuration, query syntax, ranking. A posting table of our own behaves
identically on both and keeps matching semantics ours: case-insensitive whole
terms plus bounded prefix expansion, which is what makes it feel like grep for
IDs, code identifiers, and exact names that embeddings blur.

Ranking is BM25-flavoured but computed in Python over only the matched
postings: primary sort is how many distinct query terms a chunk matched, so an
AND-ish answer outranks a single-term coincidence; secondary is an idf-weighted
term-frequency score. Quoted segments are phrases — candidates must contain
them verbatim (case-insensitive), verified against chunk text after ranking,
because positions are not stored in v1.

Known limitation, accepted for v1: tokenization splits on non-word characters
and drops tokens over 100 chars, so unsegmented scripts (CJK) index poorly.
"""
from __future__ import annotations

import math
import re
from collections import Counter, defaultdict
from typing import Dict, List, Tuple

from asgiref.sync import sync_to_async
from django.db.models import Q

from inference.engine import SearchResult, _chunk_text
from inference.models import DocumentChunk, IndexedTerm
from workflow_backend.thresholds import CHUNK_OVERLAP, CHUNK_SIZE

from .base import IngestResult, RetrievalBackend

#: Longest token kept. Anything longer is not a word; indexing it would let a
#: corrupted blob pollute every prefix search sharing its first letters.
MAX_TERM_LEN = 100

#: Shortest term that earns prefix expansion. Prefix-matching one- and
#: two-letter queries explodes the candidate set for near-zero precision.
PREFIX_MIN_LEN = 3

#: Ceiling on postings rows examined per search. The aggregation below is
#: linear in this number; without a cap a common term in a huge KB could drag
#: the whole index through Python.
POSTING_SCAN_LIMIT = 20_000

#: Raw idf-weighted tf score → score/(score+K) lands in (0, 1), so keyword
#: hits stay shape-compatible with the cosine similarities consumers expect.
_SCORE_NORM_K = 8.0

#: Prefix-expanded postings carry half weight against the term they expand:
#: at equal frequency, a literal hit must outrank "starts with" — the whole
#: point of grep-shaped search is knowing the string you are looking for.
PREFIX_TF_WEIGHT = 0.5

_SPLIT_RE = re.compile(r'[^\w]+', re.UNICODE)
_PHRASE_RE = re.compile(r'"([^"]+)"')


def tokenize(text: str) -> List[str]:
    """Lowercase, split on non-word characters, drop empties and monsters."""
    if not text:
        return []
    return [
        t for t in _SPLIT_RE.split(text.lower())
        if t and len(t) <= MAX_TERM_LEN
    ]


def parse_query(query: str) -> Tuple[List[str], List[str]]:
    """Split a raw query into bare terms and quoted phrases."""
    phrases = [p.strip().lower() for p in _PHRASE_RE.findall(query) if p.strip()]
    bare = _PHRASE_RE.sub(' ', query)
    return tokenize(bare), phrases


def _query_term_for(indexed_term: str, terms: List[str]) -> str | None:
    """The first query term this indexed posting satisfies: itself exactly, or
    as a prefix expansion when the query term is long enough to earn one."""
    for qt in terms:
        if indexed_term == qt:
            return qt
    for qt in terms:
        if len(qt) >= PREFIX_MIN_LEN and indexed_term.startswith(qt):
            return qt
    return None


def rank_postings(
    rows: List[Tuple[str, int, int, int]],
    terms: List[str],
    top_k: int,
) -> List[Tuple[int, float]]:
    """
    Aggregate posting rows → ranked [(chunk_id, score)], best first.

    A row is (term, chunk_id, document_id, tf). Distinct query terms matched
    leads the sort — an answer hitting all three words beats a deeper single-
    word match — and the idf-weighted tf sum breaks ties. Document frequency
    is derived from the scanned rows themselves: within one KB's index, the
    rows a term produced *are* its document frequency.
    """
    # chunk_id → {query_term → effective tf}
    hits: Dict[int, Dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for term, chunk_id, _doc_id, tf in rows:
        qt = _query_term_for(term, terms)
        if qt is not None:
            weight = 1.0 if term == qt else PREFIX_TF_WEIGHT
            hits[chunk_id][qt] += tf * weight

    if not hits:
        return []

    # df per query term across candidate chunks.
    df: Dict[str, int] = Counter()
    for per_term in hits.values():
        df.update(per_term.keys())

    n_chunks = len(hits)
    scored: List[Tuple[int, int, float]] = []
    for chunk_id, per_term in hits.items():
        weight = 0.0
        for qt, tf in per_term.items():
            idf = math.log(1.0 + n_chunks / max(df[qt], 1))
            weight += idf * tf / (tf + 1.2)
        scored.append((chunk_id, len(per_term), weight))

    scored.sort(key=lambda s: (-s[1], -s[2]))
    return [(cid, w / (_SCORE_NORM_K + w)) for cid, _m, w in scored[:top_k]]


class FullTextBackend(RetrievalBackend):
    backend_name = 'fulltext'

    # ---- ingest -------------------------------------------------------------

    async def ingest(self, document) -> IngestResult:
        content = document.content_text or ''

        def _index():
            # Idempotent re-ingest: old chunks take their postings with them
            # (IndexedTerm cascades off DocumentChunk).
            DocumentChunk.objects.filter(document=document).delete()

            texts = _chunk_text(content, CHUNK_SIZE, CHUNK_OVERLAP)
            chunks = DocumentChunk.objects.bulk_create([
                DocumentChunk(
                    document=document,
                    chunk_index=i,
                    content=text,
                    token_count=len(tokenize(text)),
                )
                for i, text in enumerate(texts)
            ])

            postings = []
            for chunk in chunks:
                counts = Counter(tokenize(chunk.content))
                postings.extend(
                    IndexedTerm(
                        kb=self.kb,
                        document=document,
                        chunk=chunk,
                        term=term,
                        term_frequency=tf,
                    )
                    for term, tf in counts.items()
                )
            if postings:
                IndexedTerm.objects.bulk_create(postings, batch_size=1000)
            return len(chunks)

        chunk_count = await sync_to_async(_index)()
        return IngestResult(
            chunk_count=chunk_count,
            status='indexed',
            detail=f'{chunk_count} chunks indexed for keyword search',
        )

    async def remove_document(self, doc_id: int) -> bool:
        def _remove():
            qs = DocumentChunk.objects.filter(
                document_id=doc_id, document__knowledge_base=self.kb,
            )
            existed = qs.exists()
            if existed:
                qs.delete()  # postings cascade with their chunks
            return existed

        return await sync_to_async(_remove)()

    # ---- search -------------------------------------------------------------

    async def search(self, query, top_k=5, doc_id=None) -> List[SearchResult]:
        terms, phrases = parse_query(query)
        if not terms and not phrases:
            return []

        if not terms:
            return await self._phrase_scan(phrases, top_k, doc_id)

        rows = await sync_to_async(self._scan)(terms)
        if not rows:
            return []

        ranked = rank_postings(rows, terms, top_k)
        if not ranked:
            return []

        results = await sync_to_async(self._hydrate)(ranked, phrases, doc_id)
        results = results[:top_k]

        if len(rows) >= POSTING_SCAN_LIMIT and results:
            # A truncated scan and a complete one must not look alike. The
            # ranking is over a capped slice of the postings, so the last hit
            # carries the caveat rather than the caller having to know the cap
            # exists.
            results[-1].metadata = {
                **results[-1].metadata,
                'scan_truncated': True,
                'scan_limit': POSTING_SCAN_LIMIT,
            }

        return results

    def _scan(self, terms: List[str]):
        """Postings rows for exact terms plus prefix expansions, capped.

        Two details that were wrong and are load-bearing:

        `order_by` is mandatory, not cosmetic. Slicing an unordered queryset
        leaves *which* rows survive the cap to the database, so the same query
        could return different postings on consecutive calls, and the rows most
        worth keeping (highest term frequency) were as likely to be dropped as
        any other. Ordering by `-term_frequency` makes the cap a deliberate
        "keep the strongest postings" rather than "keep whatever came back".

        `startswith`, not `istartswith`: everything in this index is lowercased
        at write time by `tokenize`, and the query terms are lowercased by
        `parse_query`, so case-insensitivity is already guaranteed by the data.
        The `i` variant compiles to `UPPER(term) LIKE …` on PostgreSQL, which
        cannot use the (kb, term) index the whole design rests on.
        """
        filters = Q(kb_id=self.kb.id, term__in=terms)
        for t in terms:
            if len(t) >= PREFIX_MIN_LEN:
                filters |= Q(kb_id=self.kb.id, term__startswith=t)
        return list(
            IndexedTerm.objects.filter(filters)
            .order_by('-term_frequency', 'chunk_id')
            .values_list('term', 'chunk_id', 'document_id', 'term_frequency')
            [:POSTING_SCAN_LIMIT]
        )

    def _hydrate(
        self,
        ranked: List[Tuple[int, float]],
        phrases: List[str],
        doc_id: int | None,
    ) -> List[SearchResult]:
        scores = dict(ranked)
        chunks = (
            DocumentChunk.objects.filter(id__in=scores)
            .select_related('document')
        )
        by_id = {c.id: c for c in chunks}

        results: List[SearchResult] = []
        for cid, score in sorted(ranked, key=lambda r: -r[1]):
            chunk = by_id.get(cid)
            if chunk is None:
                continue
            if doc_id is not None and chunk.document_id != doc_id:
                continue
            if phrases:
                haystack = chunk.content.lower()
                if not all(p in haystack for p in phrases):
                    continue
            results.append(SearchResult(
                document_id=chunk.document_id,
                chunk_id=f'chunk_{cid}',
                content=chunk.content,
                score=score,
                metadata={'name': chunk.document.name, 'match': 'keyword'},
            ))
        return results

    async def _phrase_scan(
        self, phrases: List[str], top_k: int, doc_id: int | None,
    ) -> List[SearchResult]:
        """
        All-phrases queries skip term ranking entirely: a substring pass over
        the KB's chunks. Rare by construction (the model usually sends terms
        too), and bounded by how much chunked text one KB holds.
        """

        def _find():
            out = []
            qs = (
                DocumentChunk.objects.filter(document__knowledge_base=self.kb)
                .select_related('document')
            )
            for chunk in qs.iterator():
                if doc_id is not None and chunk.document_id != doc_id:
                    continue
                hay = chunk.content.lower()
                if all(p in hay for p in phrases):
                    out.append((chunk, min(len(p) for p in phrases)))
                if len(out) >= top_k:
                    break
            out.sort(key=lambda h: -h[1])
            return out

        hits = await sync_to_async(_find)()
        return [
            SearchResult(
                document_id=chunk.document_id,
                chunk_id=f'chunk_{chunk.id}',
                content=chunk.content,
                score=min(1.0, best / 50),
                metadata={'name': chunk.document.name, 'match': 'phrase'},
            )
            for chunk, best in hits[:top_k]
        ]
