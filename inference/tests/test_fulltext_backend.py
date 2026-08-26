"""
The keyword inverted index: ingest → postings → ranked search, and the ways
documents leave without leaving stale terms behind.

These run against the real ORM on the test SQLite DB — no embedder, no FAISS,
which is rather the point of the backend.
"""
from __future__ import annotations

import asyncio

from django.contrib.auth import get_user_model
from django.test import TestCase, TransactionTestCase

from inference.backends import FullTextBackend, get_ingest_backends
from inference.backends.fulltext import parse_query, rank_postings, tokenize
from inference.models import Document, DocumentChunk, IndexedTerm, KnowledgeBase


def _run(coro):
    return asyncio.run(coro)


class TokenizeTests(TestCase):
    def test_lowercases_and_splits_on_punctuation(self):
        # Underscore deliberately survives: snake_case identifiers stay whole
        # and remain reachable through prefix expansion.
        self.assertEqual(
            tokenize('Invoice INV-0042.Total due!'),
            ['invoice', 'inv', '0042', 'total', 'due'],
        )
        self.assertEqual(tokenize('total_due_amount'), ['total_due_amount'])

    def test_drops_overlong_tokens(self):
        monster = 'x' * 500
        self.assertEqual(tokenize(f'ok {monster} ok'), ['ok', 'ok'])

    def test_empty_input_is_empty(self):
        self.assertEqual(tokenize(''), [])
        self.assertEqual(tokenize(None), [])


class ParseQueryTests(TestCase):
    def test_extracts_quoted_phrases(self):
        terms, phrases = parse_query('find "total due" today')
        self.assertEqual(phrases, ['total due'])
        self.assertIn('today', terms)
        self.assertNotIn('total', terms)  # phrase words are not bare terms

    def test_plain_query_has_no_phrases(self):
        terms, phrases = parse_query('no quotes here')
        self.assertEqual(phrases, [])
        self.assertEqual(terms, ['no', 'quotes', 'here'])


class RankPostingsTests(TestCase):
    def test_more_distinct_terms_matched_outranks_deeper_single_term(self):
        rows = [
            # chunk 1: one query term, high tf
            ('alpha', 1, 10, 50),
            # chunk 2: two different query terms once each
            ('alpha', 2, 11, 1),
            ('beta', 2, 11, 1),
        ]
        ranked = rank_postings(rows, ['alpha', 'beta'], top_k=5)
        self.assertEqual(ranked[0][0], 2)

    def test_prefix_expansion_matches_without_beating_literal(self):
        rows = [
            # 'invoice2024' expands query term 'invoice' (prefix)
            ('invoice2024', 3, 12, 10),
            # another chunk literally contains 'invoice'
            ('invoice', 4, 13, 10),
        ]
        ranked = rank_postings(rows, ['invoice'], top_k=5)
        self.assertEqual(len(ranked), 2)
        self.assertEqual(ranked[0][0], 4)  # literal beats expansion at equal tf

    def test_scores_are_bounded_in_unit_range(self):
        rows = [('word', i, i, 1000) for i in range(1, 6)]
        for _cid, score in rank_postings(rows, ['word'], top_k=5):
            self.assertGreater(score, 0.0)
            self.assertLess(score, 1.0)

    def test_unrelated_postings_are_ignored(self):
        rows = [('unrelated', 1, 1, 5)]
        self.assertEqual(rank_postings(rows, ['needle'], top_k=5), [])


class FullTextBackendTests(TransactionTestCase):
    """
    TransactionTestCase on purpose: the backend runs its ORM writes through
    sync_to_async, i.e. on a different connection than the test's own. Under
    plain TestCase that second connection hits SQLite's write lock held by
    the surrounding test transaction; under autocommit nothing holds it.
    """
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username='fulltexter', email='f@example.test', password='x'
        )
        self.kb = KnowledgeBase.objects.create(
            user=self.user, name='Grep corpus', backend='fulltext',
        )
        self.backend = FullTextBackend(self.kb)

    def _document(self, name: str, content: str) -> Document:
        return Document.objects.create(
            user=self.user,
            knowledge_base=self.kb,
            name=name,
            file_type='txt',
            file_size=len(content),
            content_text=content,
        )

    def _ingest(self, doc: Document):
        return _run(self.backend.ingest(doc))

    def test_registry_routes_fulltext_kb_to_keyword_backend(self):
        backends = get_ingest_backends(self.kb)
        self.assertEqual([type(b) for b in backends], [FullTextBackend])

    def test_ingest_creates_chunks_and_postings(self):
        doc = self._document('a.txt', 'the quick brown fox jumps over the lazy dog')
        result = self._ingest(doc)

        self.assertEqual(result.status, 'indexed')
        self.assertEqual(result.chunk_count, 1)
        self.assertEqual(DocumentChunk.objects.filter(document=doc).count(), 1)
        self.assertGreater(
            IndexedTerm.objects.filter(document=doc).count(), 0
        )
        posting = IndexedTerm.objects.get(document=doc, term='fox')
        self.assertEqual(posting.term_frequency, 1)

    def test_search_finds_exact_term_with_document_name(self):
        doc = self._document('report.txt', 'margins improved to 4.8 percent this quarter')
        self._ingest(doc)

        results = _run(self.backend.search('percent', top_k=5))
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].document_id, doc.id)
        self.assertEqual(results[0].metadata['name'], 'report.txt')
        self.assertEqual(results[0].metadata['match'], 'keyword')

    def test_search_is_case_insensitive_both_ways(self):
        doc = self._document('case.txt', 'GSTIN 27AAPFU0939F1ZV verified')
        self._ingest(doc)

        results = _run(self.backend.search('gstin', top_k=5))
        self.assertEqual(len(results), 1)

    def test_multi_word_query_prefers_chunk_hitting_all_terms(self):
        hit_all = self._document('all.txt', 'quarterly revenue exceeded every forecast')
        miss = self._document('one.txt', 'revenue was mentioned once here')
        self._ingest(hit_all)
        self._ingest(miss)

        results = _run(self.backend.search('quarterly revenue forecast', top_k=5))
        self.assertTrue(results)
        self.assertEqual(results[0].document_id, hit_all.id)

    def test_prefix_expansion_finds_identifier_variants(self):
        doc = self._document('ids.txt', 'references invoice_2024_001 and invoice_2024_002')
        self._ingest(doc)

        results = _run(self.backend.search('invoice', top_k=5))
        self.assertEqual(len(results), 1)

    def test_quoted_phrase_requires_verbatim_match(self):
        hit = self._document('phrase.txt', 'the total due amount was settled')
        miss = self._document('scrambled.txt', 'due total the amounts were settled')
        self._ingest(hit)
        self._ingest(miss)

        results = _run(self.backend.search('"total due"', top_k=5))
        self.assertEqual([r.document_id for r in results], [hit.id])

    def test_phrase_plus_terms_combine(self):
        doc = self._document('combo.txt', 'payment status shows overdue balance pending')
        other = self._document('partial.txt', 'payment received, nothing is late here')
        self._ingest(doc)
        self._ingest(other)

        # Both chunks contain 'payment'; only one contains the verbatim
        # phrase, and only that one may survive.
        results = _run(self.backend.search('"overdue balance" payment', top_k=5))
        self.assertEqual([r.document_id for r in results], [doc.id])

    def test_no_results_for_unknown_terms(self):
        self._ingest(self._document('x.txt', 'ordinary text'))
        results = _run(self.backend.search('zzzqqq'))
        self.assertEqual(results, [])

    def test_empty_query_returns_nothing(self):
        self.assertEqual(_run(self.backend.search('')), [])
        self.assertEqual(_run(self.backend.search('"  "')), [])

    def test_reingest_replaces_old_postings(self):
        doc = self._document('churn.txt', 'original content about shipping schedules')
        self._ingest(doc)

        doc.content_text = 'replacement content about billing invoices'
        doc.save()
        self._ingest(doc)

        self.assertEqual(_run(self.backend.search('shipping')), [])
        self.assertEqual(_run(self.backend.search('billing'))[0].document_id, doc.id)
        self.assertEqual(IndexedTerm.objects.filter(document=doc).count(),
                         len(set(tokenize('replacement content about billing invoices'))))

    def test_remove_document_clears_chunks_and_postings(self):
        doc = self._document('gone.txt', 'vanishing act here')
        self._ingest(doc)

        removed = _run(self.backend.remove_document(doc.id))
        self.assertTrue(removed)
        self.assertEqual(DocumentChunk.objects.filter(document=doc).count(), 0)
        self.assertEqual(IndexedTerm.objects.filter(document=doc).count(), 0)
        self.assertEqual(_run(self.backend.search('vanishing')), [])

    def test_remove_document_twice_reports_false(self):
        doc = self._document('once.txt', 'only indexed once')
        self._ingest(doc)
        self.assertTrue(_run(self.backend.remove_document(doc.id)))
        self.assertFalse(_run(self.backend.remove_document(doc.id)))

    def test_removing_other_kb_document_leaves_this_kb_alone(self):
        doc = self._document('mine.txt', 'stays put')
        self._ingest(doc)

        other_user = get_user_model().objects.create_user(
            username='other', email='o@example.test', password='x'
        )
        other_kb = KnowledgeBase.objects.create(user=other_user, name='Other')
        stranger_doc = Document.objects.create(
            user=other_user,
            knowledge_base=other_kb,
            name='stranger.txt',
            file_type='txt',
            file_size=10,
            content_text='stranger content stays put too',
        )

        # Removing the stranger's doc id through *this* KB must not touch ours.
        removed = _run(self.backend.remove_document(stranger_doc.id))
        self.assertFalse(removed)
        self.assertTrue(DocumentChunk.objects.filter(document=doc).exists())

    def test_deleting_document_cascades_everything(self):
        doc = self._document('cascade.txt', 'cascade all the way down')
        self._ingest(doc)
        doc_id = doc.id
        self.assertGreater(DocumentChunk.objects.filter(document=doc).count(), 0)
        self.assertGreater(IndexedTerm.objects.filter(document=doc).count(), 0)

        # Django 5.2 nulls the PK on delete — query by captured id afterwards.
        doc.delete()
        self.assertFalse(DocumentChunk.objects.filter(document_id=doc_id).exists())
        self.assertEqual(IndexedTerm.objects.count(), 0)

    def test_long_content_produces_multiple_ordered_chunks(self):
        content = ('. '.join(f'sentence number {i} about logistics' for i in range(80)))
        doc = self._document('long.txt', content)
        result = self._ingest(doc)

        chunks = list(
            DocumentChunk.objects.filter(document=doc).order_by('chunk_index')
        )
        self.assertGreater(len(chunks), 1)
        self.assertEqual(result.chunk_count, len(chunks))
