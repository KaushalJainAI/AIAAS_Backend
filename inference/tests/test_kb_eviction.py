"""Idle-eviction of in-memory KBs (KnowledgeBaseManager + HNSWKnowledgeBase).

Covers the eviction predicate (idle past TTL, no in-flight ops, not
re-indexing), the manager sweep, and transparent reload after eviction.
No network or FAISS index round-trips: eligibility is pure state.
"""
import time
from unittest import mock

from django.test import SimpleTestCase

from inference.engine import HNSWKnowledgeBase, KnowledgeBaseManager


def _aged_kb(kb_id: int = 1, idle_seconds: float = 301.0) -> HNSWKnowledgeBase:
    kb = HNSWKnowledgeBase(kb_id, '')
    kb._last_used = time.monotonic() - idle_seconds
    return kb


class EvictionEligibilityTests(SimpleTestCase):
    def test_fresh_instance_is_not_evictable(self):
        self.assertFalse(HNSWKnowledgeBase(1, '').is_evictable())

    def test_idle_past_ttl_is_evictable(self):
        self.assertTrue(_aged_kb().is_evictable())

    def test_in_flight_op_blocks_eviction(self):
        kb = _aged_kb()
        kb._op_begin()
        try:
            self.assertFalse(kb.is_evictable())
        finally:
            kb._op_end()
        # _op_begin touched last_used, so re-age to isolate the pin itself.
        kb._last_used = time.monotonic() - 301
        self.assertTrue(kb.is_evictable())

    def test_op_end_on_failure_still_unpins(self):
        kb = _aged_kb()
        kb._op_begin()
        kb._op_end()  # e.g. an exception path already handled by try/finally
        kb._last_used = time.monotonic() - 301
        self.assertTrue(kb.is_evictable())

    def test_reindexing_blocks_eviction_even_when_idle(self):
        kb = _aged_kb()
        kb._reindexing = True
        self.assertFalse(kb.is_evictable())


class ManagerSweepTests(SimpleTestCase):
    def setUp(self):
        self.manager = KnowledgeBaseManager()

    def test_sweep_removes_only_eligible_instances(self):
        stale = self.manager.get(1)
        stale._last_used = time.monotonic() - 301

        busy = self.manager.get(2)
        busy._last_used = time.monotonic() - 301
        busy._op_begin()

        evicted = self.manager.evict_idle()

        self.assertEqual(evicted, [1])
        with self.manager._registry_lock:
            self.assertNotIn(1, self.manager._kbs)
            self.assertIn(2, self.manager._kbs)
        busy._op_end()

    def test_get_after_eviction_returns_fresh_uninitialized_instance(self):
        old = self.manager.get(7)
        old._last_used = time.monotonic() - 301
        self.manager.evict_idle()

        fresh = self.manager.get(7)

        self.assertIsNot(fresh, old)
        self.assertFalse(fresh._initialized)

    def test_explicit_delete_path_evict_ignores_activity(self):
        kb = self.manager.get(9)
        kb._op_begin()
        self.manager.evict(9)  # delete-path eviction must always win
        with self.manager._registry_lock:
            self.assertNotIn(9, self.manager._kbs)


class ReloadAfterEvictionTests(SimpleTestCase):
    """An evicted KB re-initializes through the normal disk/S3 path.

    Django runs async test methods on their own event loop natively.
    """

    async def test_search_after_eviction_reinitializes_and_answers(self):
        import numpy as np
        import tempfile
        from pathlib import Path
        from django.conf import settings

        with tempfile.TemporaryDirectory() as tmp:
            stub_embedder = mock.Mock()
            # Read the live dimension rather than repeating a literal: the stub
            # feeds a real FAISS index, which asserts on `d`, so a hardcoded
            # width turns every embedding-model swap into a test failure that
            # says "AssertionError" and nothing about the actual change.
            from inference.engine import EMBEDDING_DIM

            vec = np.ones(EMBEDDING_DIM, dtype='float32')
            stub_embedder.encode.return_value = [vec / np.linalg.norm(vec)]

            with mock.patch.object(settings, 'FAISS_INDEX_DIR', Path(tmp)), \
                 mock.patch('inference.engine.get_global_embedder',
                            new=mock.AsyncMock(return_value=stub_embedder)):
                manager = KnowledgeBaseManager()
                kb = manager.get(42)
                kb._op_begin()
                await kb.add_document(doc_id=1, content='hello world', metadata={})
                kb._op_end()

                # Simulate the sweeper dropping it after TTL.
                kb._last_used = time.monotonic() - 301
                self.assertEqual(manager.evict_idle(), [42])

                reloaded = manager.get(42)
                self.assertIsNot(reloaded, kb)

                results = await reloaded.search('hello world')
                self.assertEqual(len(results), 1)
                self.assertEqual(results[0].document_id, 1)
