"""
The knowledge tools the agent chooses between.

The backend machinery has its own tests; these cover what the model actually
experiences — which tool answers which kind of KB, and that a misrouted call
comes back as routing advice rather than an error. A tool that fails on
misroute teaches the model to avoid retrieval; advice keeps it searching.
"""
from __future__ import annotations

import json

from asgiref.sync import async_to_sync
from django.contrib.auth import get_user_model
from django.test import TransactionTestCase

from chat.tools.knowledge import (
    keyword_search,
    knowledge_base_search,
    list_documents,
    list_knowledge_bases,
    read_document,
)
from inference.models import Document, KnowledgeBase


class KnowledgeToolBase(TransactionTestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username='knower', email='k@example.test', password='x'
        )
        self.context = {'user_id': self.user.id, 'session_id': 's', 'turn_id': 't'}

    def _kb(self, backend: str, name: str = None) -> KnowledgeBase:
        return KnowledgeBase.objects.create(
            user=self.user,
            name=name or f'{backend} kb',
            backend=backend,
        )

    def _document(self, kb: KnowledgeBase, name: str, content: str) -> Document:
        return Document.objects.create(
            user=self.user,
            knowledge_base=kb,
            name=name,
            file_type='txt',
            file_size=len(content),
            content_text=content,
            status='stored' if kb.backend == 'raw' else 'indexed',
        )


class ListKnowledgeBasesTests(KnowledgeToolBase):
    def test_reports_each_kb_retrieval_backend(self):
        self._kb('vector', 'Semantic one')
        self._kb('fulltext', 'Grep corpus')

        out = json.loads(async_to_sync(list_knowledge_bases)({}, self.context))
        by_name = {kb['name']: kb for kb in out['knowledge_bases']}
        self.assertEqual(by_name['Semantic one']['retrieval'], 'semantic')
        self.assertEqual(by_name['Grep corpus']['retrieval'], 'keyword')


class KeywordSearchTests(KnowledgeToolBase):
    def test_finds_exact_terms_in_fulltext_kb(self):
        kb = self._kb('fulltext')
        doc = self._document(kb, 'invoice.txt', 'invoice total due amount 4200 rupees')
        from inference.backends.fulltext import FullTextBackend
        async_to_sync(FullTextBackend(kb).ingest)(doc)

        out = json.loads(async_to_sync(keyword_search)(
            {'query': 'invoice', 'kb_id': kb.id}, self.context,
        ))
        self.assertEqual(out['status'], 'success')
        self.assertEqual(out['results'][0]['document_id'], doc.id)
        self.assertEqual(out['results'][0]['document_name'], 'invoice.txt')

    def test_vector_kb_returns_routing_advice_not_error(self):
        kb = self._kb('vector', 'vector kb')
        self._document(kb, 'a.txt', 'plain text content here')

        out = json.loads(async_to_sync(keyword_search)(
            {'query': 'anything', 'kb_id': kb.id}, self.context,
        ))
        self.assertEqual(out['status'], 'not_keyword_indexed')
        # The advice must name the tool that *does* work.
        self.assertIn('knowledge_base_search', out['message'])

    def test_raw_kb_points_at_the_browse_tools(self):
        kb = self._kb('raw', 'raw kb')
        self._document(kb, 'contract.txt', 'whole contract text')

        out = json.loads(async_to_sync(keyword_search)(
            {'query': 'contract', 'kb_id': kb.id}, self.context,
        ))
        self.assertEqual(out['status'], 'no_search_index')
        self.assertIn('list_documents', out['message'])
        self.assertIn('read_document', out['message'])

    def test_another_users_kb_is_invisible(self):
        other = get_user_model().objects.create_user(
            username='elsewhere', email='e@example.test', password='x'
        )
        foreign_kb = KnowledgeBase.objects.create(user=other, name='not yours')

        out = json.loads(async_to_sync(keyword_search)(
            {'query': 'term', 'kb_id': foreign_kb.id}, self.context,
        ))
        self.assertIn('error', out)

    def test_default_kb_used_when_no_id_given(self):
        default = KnowledgeBase.objects.create(
            user=self.user, name='Default', is_default=True, backend='fulltext',
        )
        doc = self._document(default, 'notes.txt', 'quarterly figures attached within')
        from inference.backends.fulltext import FullTextBackend
        async_to_sync(FullTextBackend(default).ingest)(doc)

        out = json.loads(async_to_sync(keyword_search)({'query': 'figures'}, self.context))
        self.assertEqual(out['results'][0]['document_id'], doc.id)


class KnowledgeBaseSearchRoutingTests(KnowledgeToolBase):
    """knowledge_base_search is semantic; its misroutes must advise."""

    def test_raw_kb_tells_the_model_how_to_read_instead(self):
        kb = self._kb('raw', 'raw kb')
        self._document(kb, 'doc.txt', 'text lives here')

        out = json.loads(async_to_sync(knowledge_base_search)(
            {'query': 'meaning of anything', 'kb_id': kb.id}, self.context,
        ))
        self.assertEqual(out['status'], 'no_search_index')
        self.assertIn(f"kb_id={kb.id}", out['message'])

    def test_fulltext_kb_reroutes_and_labels_the_results(self):
        kb = self._kb('fulltext', 'grep kb')
        doc = self._document(kb, 'data.txt', 'sensor reading 4.8 volts recorded')
        from inference.backends.fulltext import FullTextBackend
        async_to_sync(FullTextBackend(kb).ingest)(doc)

        out = json.loads(async_to_sync(knowledge_base_search)(
            {'query': 'volts', 'kb_id': kb.id}, self.context,
        ))
        # The search still ran — but the model is told it got keyword hits.
        self.assertEqual(out['status'], 'success')
        self.assertEqual(out['results'][0]['document_id'], doc.id)
        self.assertIn('keyword-indexed', out['note'])


class ListDocumentsTests(KnowledgeToolBase):
    def test_lists_documents_with_ids_for_reading(self):
        kb = self._kb('raw', 'raw kb')
        self._document(kb, 'first.txt', 'a')
        self._document(kb, 'second.txt', 'b')

        out = json.loads(async_to_sync(list_documents)({'kb_id': kb.id}, self.context))
        names = [d['name'] for d in out['documents']]
        self.assertEqual(names, ['second.txt', 'first.txt'])  # newest first
        for entry in out['documents']:
            self.assertIn('id', entry)

    def test_truncates_past_cap_and_says_so(self):
        kb = self._kb('raw', 'raw kb')
        for i in range(55):
            self._document(kb, f'doc{i}.txt', f'content {i}')

        out = json.loads(async_to_sync(list_documents)({'kb_id': kb.id}, self.context))
        self.assertEqual(out['count'], 50)
        self.assertTrue(out['truncated'])


class ReadDocumentTests(KnowledgeToolBase):
    def setUp(self):
        super().setUp()
        self.kb = self._kb('raw', 'raw kb')

    def test_reads_whole_short_document(self):
        doc = self._document(self.kb, 'short.txt', 'the entire agreement text')

        result = async_to_sync(read_document)({'document_id': doc.id}, self.context)
        self.assertIn('entire agreement text', result)
        self.assertIn('End of document.', result)

    def test_long_document_pages_with_a_working_offset(self):
        # Distinct content per stretch, sized to span exactly two windows.
        body = '\n'.join(f'line {i}: the quick brown fox {i}' for i in range(500))
        doc = self._document(self.kb, 'long.txt', body)

        first = async_to_sync(read_document)({'document_id': doc.id}, self.context)
        self.assertIn('characters 0-', first)
        self.assertIn('remain', first)

        # Footer numbers carry thousands separators; parse around them.
        raw_offset = first.rsplit('offset=', 1)[1].split('.')[0]
        offset = int(raw_offset.replace(',', ''))
        second = async_to_sync(read_document)(
            {'document_id': doc.id, 'offset': offset}, self.context,
        )
        self.assertNotEqual(first[:200], second[:200])
        self.assertIn('End of document.', second)

    def test_offset_past_end_says_so(self):
        doc = self._document(self.kb, 'tiny.txt', 'short')
        result = async_to_sync(read_document)(
            {'document_id': doc.id, 'offset': 10_000}, self.context,
        )
        self.assertIn('past the end', result)

    def test_missing_text_is_reported_not_empty_string(self):
        doc = self._document(self.kb, 'empty.bin', '')
        result = async_to_sync(read_document)({'document_id': doc.id}, self.context)
        self.assertIn('no extracted text', result)

    def test_foreign_private_document_is_not_readable(self):
        other = get_user_model().objects.create_user(
            username='privateowner', email='p@example.test', password='x'
        )
        foreign_doc = Document.objects.create(
            user=other,
            name='secret.txt',
            file_type='txt',
            file_size=5,
            content_text='classified material',
        )
        result = async_to_sync(read_document)({'document_id': foreign_doc.id}, self.context)
        self.assertIn('error', json.loads(result))

    def test_shared_document_from_public_library_is_readable(self):
        other = get_user_model().objects.create_user(
            username='sharer', email='s@example.test', password='x'
        )
        shared_doc = Document.objects.create(
            user=other,
            name='public.txt',
            file_type='txt',
            file_size=12,
            content_text='public knowledge',
            sharing_mode='shared_read',
        )
        result = async_to_sync(read_document)({'document_id': shared_doc.id}, self.context)
        self.assertIn('public knowledge', result)


class TrashedDocumentsAreInvisibleToToolsTests(TransactionTestCase):
    """The payoff for making trash a default-manager state.

    Neither tool was edited when the recycle bin landed. `list_documents` and
    `read_document` both go through `Document.objects`, which is `LiveManager`,
    so a trashed file left the agent's view without anybody remembering to make
    it. These tests are what make that inheritance a checked property rather
    than a happy accident.
    """

    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username='trashtools', password='pw')
        self.kb = KnowledgeBase.objects.create(
            user=self.user, name='Raw', is_default=True,
            backend=KnowledgeBase.BACKEND_RAW,
        )
        self.doc = Document.objects.create(
            user=self.user, name='invoice.txt', file_type='txt', file_size=1,
            content_text='total due 48200', knowledge_base=self.kb, status='stored',
        )
        self.context = {'user_id': self.user.id}

    def _trash(self):
        from inference import recycle
        recycle.trash(self.user, documents=[self.doc])

    def test_list_documents_stops_showing_it(self):
        from chat.tools.knowledge import list_documents

        before = json.loads(async_to_sync(list_documents)({}, self.context))
        self.assertEqual([d['id'] for d in before['documents']], [self.doc.id])

        self._trash()

        after = json.loads(async_to_sync(list_documents)({}, self.context))
        self.assertEqual(after['documents'], [])

    def test_read_document_stops_returning_its_text(self):
        from chat.tools.knowledge import read_document

        self._trash()

        result = async_to_sync(read_document)(
            {'document_id': self.doc.id}, self.context)

        self.assertNotIn('48200', result)

    def test_restoring_brings_it_back_to_the_agent(self):
        from chat.tools.knowledge import list_documents
        from inference import recycle

        self._trash()
        recycle.restore(self.user, document_ids=[self.doc.id])

        after = json.loads(async_to_sync(list_documents)({}, self.context))
        self.assertEqual([d['id'] for d in after['documents']], [self.doc.id])
