"""
Unit tests for inference/engine.py — pure helpers that don't require
the embedder model, FAISS, or S3.
"""
from __future__ import annotations

from django.test import SimpleTestCase

from inference.engine import _chunk_text


class ChunkTextTests(SimpleTestCase):
    def test_empty_text_returns_empty(self):
        self.assertEqual(_chunk_text("", 100, 20), [])

    def test_short_text_single_chunk(self):
        out = _chunk_text("hello world", 100, 10)
        self.assertEqual(out, ["hello world"])

    def test_chunks_respect_size_budget(self):
        text = "A" * 500
        chunks = _chunk_text(text, chunk_size=100, overlap=20)
        # Each chunk must not exceed chunk_size by much.
        for c in chunks:
            self.assertLessEqual(len(c), 100)

    def test_overlap_larger_than_chunk_size_does_not_hang(self):
        # Regression guard: pathological overlap >= chunk_size would have
        # caused an infinite loop before the clamp was added.
        text = "word " * 50
        chunks = _chunk_text(text, chunk_size=10, overlap=20)
        # Should terminate with some chunks.
        self.assertGreater(len(chunks), 0)

    def test_boundary_progress_guaranteed(self):
        # Zero-overlap minimum-size loop still terminates.
        text = "abcdefghij"
        chunks = _chunk_text(text, chunk_size=3, overlap=0)
        # All characters covered.
        self.assertEqual("".join(chunks), "abcdefghij")

    def test_splits_on_paragraph_boundary_when_possible(self):
        text = "A" * 60 + "\n\n" + "B" * 60
        chunks = _chunk_text(text, chunk_size=100, overlap=0)
        # First chunk should end at the paragraph boundary rather than mid-text.
        self.assertTrue(chunks[0].endswith("A") or chunks[0].endswith("A\n\n".strip()))

    def test_unicode_content_safe(self):
        # Multi-byte characters (e.g. emoji, CJK) must not corrupt slicing.
        text = "🎯" * 50 + "中文" * 50
        chunks = _chunk_text(text, chunk_size=20, overlap=5)
        # All output chunks should still be strings (no exception, no corruption).
        joined = "".join(chunks)
        self.assertIn("🎯", joined)
        self.assertIn("中", joined)

    def test_drops_empty_chunks_after_strip(self):
        # Whitespace-only segments shouldn't make it into the output.
        text = "real\n\n\n\n   \n\nmore real"
        chunks = _chunk_text(text, chunk_size=20, overlap=0)
        for c in chunks:
            self.assertTrue(c.strip(), f"empty chunk produced: {c!r}")


# ─────────────────────────────────────────────────────────────────────────
# RAGPipeline result-shape compatibility
# ─────────────────────────────────────────────────────────────────────────
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

from inference.engine import RAGPipeline, SearchResult


def _run(coro):
    return asyncio.run(coro)


class RAGPipelineNoContextTests(SimpleTestCase):
    def test_no_results_returns_no_context_flag(self):
        kb = MagicMock()
        kb.search = AsyncMock(return_value=[])
        pipe = RAGPipeline(kb)
        out = _run(pipe.query("hi", user_id=1))
        self.assertTrue(out.get("no_context"))
        self.assertEqual(out["sources"], [])

    def test_unknown_llm_returns_error(self):
        kb = MagicMock()
        kb.search = AsyncMock(return_value=[
            SearchResult(document_id=1, chunk_id="c1", content="hello",
                         score=0.9, metadata={}),
        ])
        pipe = RAGPipeline(kb)
        with patch("nodes.handlers.registry.get_registry") as get_reg:
            reg = MagicMock()
            reg.has_handler.return_value = False
            get_reg.return_value = reg
            out = _run(pipe.query("q", user_id=1, llm_type="nonexistent"))
        self.assertTrue(out.get("error"))


class SearchResultDataclassTests(SimpleTestCase):
    def test_default_is_image_false(self):
        r = SearchResult(document_id=1, chunk_id="c", content="x",
                         score=0.5, metadata={})
        self.assertFalse(r.is_image)

    def test_score_preserved(self):
        r = SearchResult(document_id=1, chunk_id="c", content="x",
                         score=0.42, metadata={"a": 1})
        self.assertAlmostEqual(r.score, 0.42)
        self.assertEqual(r.metadata, {"a": 1})
