"""
The backend registry and the backends whose behaviour is testable without an
embedder or FAISS.

VectorBackend delegates to engine.HNSWKnowledgeBase (covered by the engine's
own surface); here the registry mapping, the raw no-op contract, and the RRF
fusion that makes hybrid results stable are what's under test.
"""
from __future__ import annotations

import asyncio

from django.test import TestCase

from inference.backends import (
    FullTextBackend,
    HybridBackend,
    RawBackend,
    RetrievalBackend,
    VectorBackend,
    get_ingest_backends,
    get_search_backends,
)
from inference.backends.hybrid import fuse
from inference.engine import SearchResult


def _run(coro):
    return asyncio.run(coro)


def _hit(doc_id: int, content: str, score: float) -> SearchResult:
    return SearchResult(
        document_id=doc_id, chunk_id=f'c{doc_id}', content=content,
        score=score, metadata={},
    )


class FakeKB:
    """Just enough of a KB for backend construction."""
    id = 1
    backend = 'vector'
    s3_index_key = ''


class RegistryTests(TestCase):
    def _kb(self, backend: str) -> FakeKB:
        kb = FakeKB()
        kb.backend = backend
        return kb

    def test_vector_maps_to_vector_backend(self):
        self.assertEqual(
            [type(b) for b in get_ingest_backends(self._kb('vector'))],
            [VectorBackend],
        )

    def test_fulltext_ingests_and_searches_alone(self):
        kb = self._kb('fulltext')
        self.assertEqual([type(b) for b in get_ingest_backends(kb)], [FullTextBackend])
        self.assertEqual([type(b) for b in get_search_backends(kb)], [FullTextBackend])

    def test_raw_has_no_search_backends(self):
        kb = self._kb('raw')
        self.assertEqual([type(b) for b in get_ingest_backends(kb)], [RawBackend])
        # A raw KB answers queries by being read, not searched.
        self.assertEqual(get_search_backends(kb), [])

    def test_hybrid_ingests_and_searches_through_one_backend(self):
        kb = self._kb('hybrid')
        self.assertEqual([type(b) for b in get_ingest_backends(kb)], [HybridBackend])
        self.assertEqual([type(b) for b in get_search_backends(kb)], [HybridBackend])

    def test_unknown_backend_falls_back_to_nothing_rather_than_crashing(self):
        self.assertEqual(get_ingest_backends(self._kb('quantum')), [])
        self.assertEqual(get_search_backends(self._kb('quantum')), [])

    def test_backends_are_retrieval_interface_instances(self):
        for b in get_ingest_backends(self._kb('hybrid')):
            self.assertIsInstance(b, RetrievalBackend)


class RawBackendTests(TestCase):
    def setUp(self):
        self.backend = RawBackend(FakeKB())

    def test_ingest_reports_stored_not_indexed(self):
        class Doc:
            id = 7
            name = 'contract.pdf'
            user_id = 1
            content_text = 'whole agreement text'

        result = _run(self.backend.ingest(Doc()))
        self.assertEqual(result.status, 'stored')
        # Nothing was chunked — there is nothing to chunk into.
        self.assertEqual(result.chunk_count, 0)

    def test_search_is_honestly_empty(self):
        self.assertEqual(_run(self.backend.search('anything')), [])

    def test_remove_reports_nothing_indexed(self):
        self.assertFalse(_run(self.backend.remove_document(1)))


class FuseTests(TestCase):
    def test_agreement_across_lists_wins(self):
        """A hit both searches agree on outranks either list's own top pick."""
        shared = _hit(1, 'both found this', 0.9)
        lists = [
            [shared, _hit(2, 'only semantic', 0.8)],
            [_hit(3, 'only keyword', 0.9), shared],
        ]
        fused = dict(fuse(lists, top_k=3))
        best_key = max(fused, key=lambda k: fused[k])
        self.assertIn('both found this', best_key)

    def test_single_list_preserves_rank_order(self):
        lists = [[_hit(i, f'doc {i}', 0.5) for i in range(5)]]
        scores = [s for _k, s in fuse(lists, top_k=5)]
        self.assertEqual(scores, sorted(scores, reverse=True))

    def test_duplicates_within_and_across_lists_are_merged(self):
        dup = _hit(4, 'same text', 0.9)
        lists = [[dup, dup], [dup]]
        fused = fuse(lists, top_k=10)
        self.assertEqual(len(fused), 1)

    def test_scores_stay_in_unit_range_even_at_full_agreement(self):
        lists = [
            [_hit(1, 'a', 0.99), _hit(2, 'b', 0.98)],
            [_hit(1, 'a', 0.97), _hit(2, 'b', 0.96)],
        ]
        for _key, score in fuse(lists, top_k=5):
            self.assertGreaterEqual(score, 0.0)
            self.assertLessEqual(score, 1.0)


class HybridCompositionTests(TestCase):
    def test_hybrid_backend_composes_both_mechanisms(self):
        backend = HybridBackend(FakeKB())
        self.assertIsInstance(backend.vector, VectorBackend)
        self.assertIsInstance(backend.fulltext, FullTextBackend)
