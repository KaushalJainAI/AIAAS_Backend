"""
Regressions from the 2026-08-24 audit of `inference/`.

Each test here names a defect that shipped and stayed. The theme they share is
worth stating once, because it is the shape of every bug in this file: a broad
`except Exception` around code that substituted a *different answer* instead of
failing. A wrong knowledge base, an empty result list, a stale document count —
all of them looked exactly like success to the caller, which is why the
existing suite could pass at 98/98 while `/api/inference/rag/query/` had never
once searched a user's own documents.

So these assert on the thing that was indistinguishable, not on the status code
that was already right.
"""
from __future__ import annotations

import asyncio
import os
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase, TransactionTestCase
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase

from inference.backends.base import IngestResult
from inference.backends.hybrid import HybridBackend
from inference.engine import (
    SKILLS_KB_ID,
    KnowledgeBaseUnavailable,
    get_platform_knowledge_base,
    get_session_knowledge_base,
)
from inference.models import Document, ExtractedRow, ExtractionSchema, KnowledgeBase
from inference.utils import normalize_file_type


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# 1. rag_query searched the wrong KB — on every request
# ---------------------------------------------------------------------------

class RagQueryResolvesTheCallersKBTests(APITestCase):
    """
    `get_rag_pipeline` was sync and called from an async view, so its ORM read
    raised SynchronousOnlyOperation, was swallowed, and fell back to
    `get_hnsw_kb(-user_id)`. Two failures compounded: the user's documents were
    never searched, and the fallback id collided with reserved ids — user 1
    landed on the platform KB (-1) and user 2 on the skills index (-2).
    """

    def setUp(self):
        self.user = User.objects.create_user(username='ragowner', password='pw12345')
        self.client.force_authenticate(user=self.user)

    def _pipeline_kb_id_for_request(self) -> int:
        """POST to rag/query/ and report which KB the view actually opened.

        Nothing on the resolution path is stubbed — the real view calls the
        real `get_rag_pipeline`. Only FAISS is replaced, at the single seam
        where a KB id turns into an index, so the id under test is the one the
        production path computed.
        """
        seen = {}

        def _recording_get_hnsw_kb(kb_id, s3_key_prefix=''):
            seen['kb_id'] = kb_id
            return _StubKB(kb_id)

        with patch('inference.engine.get_hnsw_kb', _recording_get_hnsw_kb):
            self.client.post('/api/inference/rag/query/', {'question': 'hi'}, format='json')
        return seen.get('kb_id')

    def test_resolves_the_users_own_default_kb(self):
        kb = KnowledgeBase.objects.create(user=self.user, name='Mine', is_default=True)
        self.assertEqual(self._pipeline_kb_id_for_request(), kb.id)

    def test_never_resolves_to_a_reserved_negative_id(self):
        KnowledgeBase.objects.create(user=self.user, name='Mine', is_default=True)
        resolved = self._pipeline_kb_id_for_request()
        self.assertGreater(
            resolved, 0,
            'A user KB id must be positive — negative ids are the platform (-1), '
            'skills (-2) and session KBs, i.e. other people\'s corpora.',
        )
        self.assertNotIn(resolved, (get_platform_knowledge_base().kb_id, SKILLS_KB_ID))

    def test_creates_the_default_kb_when_the_user_has_none(self):
        self.assertFalse(KnowledgeBase.objects.filter(user=self.user).exists())
        resolved = self._pipeline_kb_id_for_request()
        kb = KnowledgeBase.objects.get(user=self.user, is_default=True)
        self.assertEqual(resolved, kb.id)


class _StubKB:
    """A KB stand-in that answers the search API without touching FAISS."""

    def __init__(self, kb_id=0):
        self.kb_id = kb_id

    async def initialize(self):
        return None

    async def search(self, *args, **kwargs):
        return []


# ---------------------------------------------------------------------------
# 2. A broken KB answered "no results"
# ---------------------------------------------------------------------------

class UnavailableKBIsAnErrorNotAnEmptyAnswerTests(APITestCase):
    """
    `initialize()` logged every failure and returned, leaving `_initialized`
    False; `_search_inner` then returned []. A missing NVIDIA_API_KEY and an
    empty corpus produced byte-identical responses — the exact failure the chat
    turn's `llm.preflight()` exists to prevent, on the retrieval side.
    """

    def setUp(self):
        self.user = User.objects.create_user(username='sadkb', password='pw12345')
        self.client.force_authenticate(user=self.user)
        self.kb = KnowledgeBase.objects.create(user=self.user, name='KB', is_default=True)

    def test_initialize_raises_rather_than_degrading(self):
        from inference.engine import HNSWKnowledgeBase

        kb = HNSWKnowledgeBase(self.kb.id)
        with patch('inference.engine.get_global_embedder',
                   side_effect=RuntimeError('NVIDIA_API_KEY is not configured')):
            with self.assertRaises(KnowledgeBaseUnavailable):
                _run(kb.initialize())

    def test_rag_search_answers_503_not_an_empty_result_list(self):
        with patch('inference.engine.get_global_embedder',
                   side_effect=RuntimeError('NVIDIA_API_KEY is not configured')):
            response = self.client.post(
                '/api/inference/rag/search/', {'query': 'anything'}, format='json',
            )

        self.assertEqual(response.status_code, 503)
        self.assertNotIn(
            'results', response.data,
            'A broken embedder must not be reported as a successful empty search.',
        )


# ---------------------------------------------------------------------------
# 3. file_type had three vocabularies and images were indexed as mojibake
# ---------------------------------------------------------------------------

class FileTypeNormalisationTests(TestCase):
    """
    The upload view took the raw extension ('png'), chat attachments used their
    own five-value set, and `Document.FILE_TYPE_CHOICES` declared a third.
    `extract_text_from_file`'s `if file_type in ('image','video')` guard never
    matched, so a PNG was read as UTF-8 with errors ignored and the resulting
    binary noise was chunked and embedded — and `extraction.py` never handed
    the pixels to the vision model.
    """

    def test_image_extensions_map_to_the_image_choice(self):
        for name in ('photo.png', 'scan.JPG', 'shot.jpeg', 'art.webp'):
            with self.subTest(name=name):
                self.assertEqual(normalize_file_type(name), 'image')

    def test_video_extensions_map_to_the_video_choice(self):
        for name in ('clip.mp4', 'demo.MOV', 'rec.webm'):
            with self.subTest(name=name):
                self.assertEqual(normalize_file_type(name), 'video')

    def test_every_result_is_a_declared_document_choice(self):
        declared = {value for value, _label in Document.FILE_TYPE_CHOICES}
        names = ['a.png', 'b.mp4', 'c.pdf', 'd.docx', 'e.csv', 'f.json',
                 'g.html', 'h.md', 'i.txt', 'unknown.xyz', 'noextension']
        for name in names:
            with self.subTest(name=name):
                self.assertIn(normalize_file_type(name), declared)

    def test_mime_type_decides_when_the_extension_does_not(self):
        self.assertEqual(normalize_file_type('scan', 'image/png'), 'image')
        self.assertEqual(normalize_file_type('clip', 'video/mp4'), 'video')

    def test_extension_wins_over_a_generic_mime(self):
        self.assertEqual(normalize_file_type('photo.png', 'application/octet-stream'), 'image')

    def test_text_extraction_skips_what_normalisation_marks_binary(self):
        from inference.utils import extract_text_from_file

        # The guard is keyed on the normalised value, so this is the pairing
        # that used to be broken: 'png' reached the else-branch and was read
        # as text.
        self.assertEqual(extract_text_from_file('/nonexistent.png', 'image'), '')


# ---------------------------------------------------------------------------
# 3b. a .docx was read as UTF-8 zip noise
# ---------------------------------------------------------------------------

class DocxExtractionTests(TestCase):
    """
    `.docx` has been in `ALLOWED_MIME_TYPES` and in `FILE_TYPE_CHOICES` since the
    first migration, and `extract_text_from_file` had no branch for it — so a
    Word document fell to the `else`, was opened as UTF-8 with `errors='ignore'`,
    and its zip container was stored as `content_text`, chunked and embedded.
    The failure was silent in both directions: the upload succeeded and the
    index filled with noise that matched nothing.
    """

    W = 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'

    def _docx(self, body_xml: str) -> str:
        """A minimal .docx on disk. Only `word/document.xml` is ever read."""
        import tempfile
        import zipfile

        path = os.path.join(tempfile.mkdtemp(), 'sample.docx')
        with zipfile.ZipFile(path, 'w') as z:
            z.writestr(
                'word/document.xml',
                f'<?xml version="1.0"?><w:document xmlns:w="{self.W}">'
                f'<w:body>{body_xml}</w:body></w:document>',
            )
        return path

    @staticmethod
    def _para(*runs: str) -> str:
        inner = ''.join(f'<w:r><w:t>{r}</w:t></w:r>' for r in runs)
        return f'<w:p>{inner}</w:p>'

    def test_paragraphs_come_back_as_text_not_zip_noise(self):
        from inference.utils import extract_text_from_file

        path = self._docx(self._para('Quarterly review') + self._para('Revenue rose.'))
        text = extract_text_from_file(path, 'docx')

        self.assertIn('Quarterly review', text)
        self.assertIn('Revenue rose.', text)
        # The tell of the old behaviour: a zip's local file header signature.
        self.assertNotIn('PK', text)

    def test_runs_within_one_paragraph_join_without_a_gap(self):
        from inference.utils import extract_docx_text

        # Word splits a styled word across runs; joining with a space would
        # invent one that the document does not contain.
        path = self._docx(self._para('un', 'break', 'able'))
        self.assertEqual(extract_docx_text(path).strip(), 'unbreakable')

    def test_a_table_row_stays_one_line(self):
        from inference.utils import extract_docx_text

        row = (
            '<w:tbl><w:tr>'
            f'<w:tc>{self._para("Q1")}</w:tc>'
            f'<w:tc>{self._para("120")}</w:tc>'
            '</w:tr></w:tbl>'
        )
        lines = extract_docx_text(self._docx(row)).splitlines()
        # One line, not two: which value belonged to which column is the whole
        # reason the table exists.
        self.assertEqual(lines, ['Q1\t120'])

    def test_a_legacy_doc_yields_nothing_rather_than_garbage(self):
        from inference.utils import extract_text_from_file

        # `normalize_file_type` files a legacy `.doc` as 'docx' too, and OLE2 is
        # not a zip. Empty is the honest answer — `vfs.read_file` already
        # explains a document with no extracted text.
        import tempfile

        path = os.path.join(tempfile.mkdtemp(), 'old.doc')
        with open(path, 'wb') as fh:
            fh.write(bytes([0xd0, 0xcf, 0x11, 0xe0, 0xa1, 0xb1, 0x1a, 0xe1]) + bytes(64))
        self.assertEqual(extract_text_from_file(path, 'docx'), '')

    def test_a_missing_file_is_empty_not_an_exception(self):
        from inference.utils import extract_docx_text

        self.assertEqual(extract_docx_text('/nonexistent/sample.docx'), '')

# ---------------------------------------------------------------------------
# 4. doc_count only ever counted up
# ---------------------------------------------------------------------------

class KBStatsFollowDeletionTests(APITestCase):
    """
    `_sync_kb_stats` ran on ingest only, and `engine.update_kb_stats` — written
    for the other end — had zero callers. `chat/tools/knowledge.py` reports
    `doc_count` to the agent, so the model was told an emptied KB still held
    documents.
    """

    def setUp(self):
        self.user = User.objects.create_user(username='counter', password='pw12345')
        self.client.force_authenticate(user=self.user)
        # `raw` so no FAISS or embedder is involved in the delete fan-out.
        self.kb = KnowledgeBase.objects.create(
            user=self.user, name='Raw', backend=KnowledgeBase.BACKEND_RAW,
        )

    def _doc(self, name: str) -> Document:
        return Document.objects.create(
            user=self.user, name=name, file_type='txt', file_size=1,
            content_text='hello', knowledge_base=self.kb, status='stored',
        )

    def test_deleting_a_document_decrements_doc_count(self):
        a, b = self._doc('a.txt'), self._doc('b.txt')
        KnowledgeBase.objects.filter(id=self.kb.id).update(doc_count=2)

        response = self.client.delete(f'/api/inference/documents/{a.id}/')
        # 200 with a body, not 204: DELETE moves the document to the recycle
        # bin, and the caller needs `purges_after_days` to say how long it
        # stays restorable. The permanent delete is the purge sweep's job.
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn('purges_after_days', response.data)

        self.kb.refresh_from_db()
        self.assertEqual(self.kb.doc_count, 1)
        self.assertTrue(Document.objects.filter(id=b.id).exists())

    def test_trashing_leaves_the_row_restorable(self):
        doc = self._doc('a.txt')
        self.client.delete(f'/api/inference/documents/{doc.id}/')

        # Gone from every listing, but still there to be restored — trash is a
        # state, not a delete.
        self.assertFalse(Document.objects.filter(id=doc.id).exists())
        self.assertTrue(Document.all_objects.filter(id=doc.id).exists())

    def test_emptying_a_kb_takes_the_count_to_zero(self):
        doc = self._doc('only.txt')
        KnowledgeBase.objects.filter(id=self.kb.id).update(doc_count=1)

        self.client.delete(f'/api/inference/documents/{doc.id}/')

        self.kb.refresh_from_db()
        self.assertEqual(self.kb.doc_count, 0)

    def test_vector_columns_are_not_zeroed_for_a_non_vector_kb(self):
        doc = self._doc('only.txt')
        KnowledgeBase.objects.filter(id=self.kb.id).update(
            doc_count=1, vector_count=0, index_size_bytes=0,
        )
        self.client.delete(f'/api/inference/documents/{doc.id}/')
        self.kb.refresh_from_db()
        self.assertEqual(self.kb.vector_count, 0)


# ---------------------------------------------------------------------------
# 5. Hybrid KBs reported 0 vectors for ever
# ---------------------------------------------------------------------------

class HybridCarriesVectorStatsUpTests(TestCase):
    """
    `HybridBackend.ingest` built a fresh IngestResult and dropped the vector
    half's `extras` — the only thing `_sync_kb_stats` looks for. A hybrid KB
    therefore reported vector_count=0 and index_size_bytes=0 no matter how much
    it held.
    """

    class _FakeKB:
        id = 1
        backend = 'hybrid'
        s3_index_key = ''

    def test_ingest_result_keeps_ntotal_and_size(self):
        backend = HybridBackend(self._FakeKB())

        async def _vector_ingest(document):
            return IngestResult(
                chunk_count=3, detail='3 vectors',
                extras={'ntotal': 42, 'index_size_bytes': 8192},
            )

        async def _fulltext_ingest(document):
            return IngestResult(chunk_count=3, detail='3 chunks')

        with patch.object(backend.vector, 'ingest', _vector_ingest), \
             patch.object(backend.fulltext, 'ingest', _fulltext_ingest):
            result = _run(backend.ingest(object()))

        self.assertEqual(result.extras.get('ntotal'), 42)
        self.assertEqual(result.extras.get('index_size_bytes'), 8192)

    def test_stats_sync_finds_the_vector_result_in_a_hybrid_ingest(self):
        from inference.tasks import DocumentIndexingService

        user = User.objects.create_user(username='hybridstats', password='pw')
        kb = KnowledgeBase.objects.create(
            user=user, name='H', backend=KnowledgeBase.BACKEND_HYBRID,
        )
        composed = IngestResult(
            chunk_count=3, detail='both',
            extras={'ntotal': 42, 'index_size_bytes': 8192},
        )

        DocumentIndexingService._sync_kb_stats(kb, [composed])

        kb.refresh_from_db()
        self.assertEqual(kb.vector_count, 42)
        self.assertEqual(kb.index_size_bytes, 8192)


# ---------------------------------------------------------------------------
# 6. The extraction rows viewset had a 500 and an unscoped FK
# ---------------------------------------------------------------------------

class ExtractedRowWriteSurfaceTests(APITestCase):
    """
    `ExtractedRowViewSet` was a full ModelViewSet whose serializer carries no
    `schema` field, so a root POST reached the DB with a null FK. Separately
    `document` was writable and unfiltered — a user could point their own row
    at anyone's document, which makes the review audit trail unverifiable.
    """

    def setUp(self):
        self.user = User.objects.create_user(username='rowowner', password='pw12345')
        self.other = User.objects.create_user(username='rowstranger', password='pw12345')
        self.client.force_authenticate(user=self.user)
        self.schema = ExtractionSchema.objects.create(
            user=self.user, name='S', fields=[{'name': 'a', 'type': 'string'}],
        )

    def _doc(self, owner) -> Document:
        return Document.objects.create(
            user=owner, name='d.txt', file_type='txt', file_size=1, content_text='x',
        )

    def test_root_post_is_refused_not_a_500(self):
        response = self.client.post(
            '/api/extraction/rows/', {'document_name': 'x', 'data': {'a': 1}}, format='json',
        )
        self.assertEqual(response.status_code, status.HTTP_405_METHOD_NOT_ALLOWED)

    def test_rows_are_still_creatable_through_their_schema(self):
        response = self.client.post(
            f'/api/extraction/schemas/{self.schema.id}/rows/',
            {'document_name': 'x', 'data': {'a': 1}, 'confidence': 0.95},
            format='json',
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    def test_patching_in_a_foreign_document_is_rejected(self):
        foreign = self._doc(self.other)
        row = ExtractedRow.objects.create(
            schema=self.schema, document_name='r', data={'a': 1},
        )

        response = self.client.patch(
            f'/api/extraction/rows/{row.id}/', {'document': foreign.id}, format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        row.refresh_from_db()
        self.assertIsNone(row.document_id)

    def test_patching_in_an_owned_document_still_works(self):
        mine = self._doc(self.user)
        row = ExtractedRow.objects.create(
            schema=self.schema, document_name='r', data={'a': 1},
        )

        response = self.client.patch(
            f'/api/extraction/rows/{row.id}/', {'document': mine.id}, format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        row.refresh_from_db()
        self.assertEqual(row.document_id, mine.id)

    def test_creating_a_row_against_a_foreign_document_is_rejected(self):
        foreign = self._doc(self.other)
        response = self.client.post(
            f'/api/extraction/schemas/{self.schema.id}/rows/',
            {'document_name': 'x', 'data': {'a': 1}, 'document': foreign.id},
            format='json',
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


# ---------------------------------------------------------------------------
# 7. The KB duplicate-name guard never ran
# ---------------------------------------------------------------------------

class DuplicateKBNameIsExplainedTests(APITestCase):
    """
    KB routes removed — former duplicate-name guard (validate_name via
    serializer context) is moot. Endpoints now 404; serializer still enforces
    uniqueness internally but is not reachable over HTTP.
    """

    def setUp(self):
        self.user = User.objects.create_user(username='dupowner', password='pw12345')
        self.client.force_authenticate(user=self.user)

    def test_create_is_now_404(self):
        KnowledgeBase.objects.create(user=self.user, name='Mine')
        response = self.client.post('/api/inference/kbs/', {'name': 'Mine'}, format='json')
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_rename_is_now_404(self):
        KnowledgeBase.objects.create(user=self.user, name='Taken')
        mine = KnowledgeBase.objects.create(user=self.user, name='Free')
        response = self.client.patch(
            f'/api/inference/kbs/{mine.id}/', {'name': 'Taken'}, format='json',
        )
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_detail_is_now_404(self):
        mine = KnowledgeBase.objects.create(user=self.user, name='Same')
        response = self.client.patch(
            f'/api/inference/kbs/{mine.id}/',
            {'name': 'Same', 'description': 'edited'}, format='json',
        )
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)


# ---------------------------------------------------------------------------
# 8. Session KB ids moved on every restart
# ---------------------------------------------------------------------------

class SessionKBIdIsStableTests(TestCase):
    """
    The synthetic id came from `hash()`, whose seed is randomised per process.
    A session's index files were written once and never found again — by a
    later process, or by a sibling ASGI worker in the same one.
    """

    def test_same_session_id_maps_to_the_same_kb_id(self):
        first = get_session_knowledge_base('session-abc').kb_id
        second = get_session_knowledge_base('session-abc').kb_id
        self.assertEqual(first, second)

    def test_the_id_does_not_depend_on_the_process_hash_seed(self):
        # Pinned: computed from blake2b, so it is reproducible across runs.
        # If this value changes, existing session indices become unreachable.
        self.assertEqual(
            get_session_knowledge_base('session-abc').kb_id,
            _expected_session_id('session-abc'),
        )

    def test_distinct_sessions_do_not_collide(self):
        ids = {get_session_knowledge_base(f'session-{i}').kb_id for i in range(200)}
        self.assertEqual(len(ids), 200)

    def test_session_ids_stay_clear_of_the_reserved_ids(self):
        for i in range(50):
            kb_id = get_session_knowledge_base(f'session-{i}').kb_id
            self.assertLess(kb_id, -10_000_000)
            self.assertNotIn(kb_id, (-1, SKILLS_KB_ID))


def _expected_session_id(session_id: str) -> int:
    import hashlib

    digest = hashlib.blake2b(session_id.encode('utf-8'), digest_size=8).digest()
    return -(int.from_bytes(digest, 'big') % 10_000_000 + 10_000_000)


# ---------------------------------------------------------------------------
# 9. Sharing raced its own worker
# ---------------------------------------------------------------------------

class ShareCommitsBeforeTheWorkerStartsTests(APITestCase):
    """
    The thread was started before `doc.save()`. It re-reads the row to copy
    `sharing_mode` into the platform KB's metadata, so it could read 'private'
    — recording a shared document as private in the shared index.
    """

    def setUp(self):
        self.user = User.objects.create_user(username='sharer', password='pw12345')
        self.client.force_authenticate(user=self.user)
        self.doc = Document.objects.create(
            user=self.user, name='s.txt', file_type='txt', file_size=1, content_text='x',
        )

    def test_the_row_is_committed_before_the_thread_is_spawned(self):
        """Asserts the *ordering*, which is the whole defect.

        Reading the row from inside the recorder is not an option — the spawn
        happens in an async view, where a sync ORM call raises. So the two
        events are recorded as they occur and the sequence is checked: the
        worker re-reads the row, so it must not start before the write lands.
        """
        import threading as real_threading

        from inference.tasks import share_document as share_target

        events = []
        real_save = Document.save

        def _recording_save(doc_self, *args, **kwargs):
            result = real_save(doc_self, *args, **kwargs)
            if doc_self.pk == self.doc.pk:
                events.append('saved')
            return result

        class _Thread(real_threading.Thread):
            """Intercepts only the share worker. asgiref's own pool threads go
            through this constructor too, and must still be real threads."""

            def __init__(self, *args, target=None, **kwargs):
                self._intercepted = target is share_target
                super().__init__(*args, target=target, **kwargs)

            def start(self):
                if not self._intercepted:
                    return super().start()
                events.append('worker_started')

        with patch.object(Document, 'save', _recording_save), \
             patch('inference.views.threading.Thread', _Thread):
            response = self.client.post(f'/api/inference/documents/{self.doc.id}/share/')

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn('worker_started', events)
        self.assertLess(
            events.index('saved'), events.index('worker_started'),
            'The share worker re-reads the row, so it must not start before '
            'the sharing_mode write is committed.',
        )

    def test_resharing_is_still_refused(self):
        self.doc.sharing_mode = 'shared_read'
        self.doc.save()
        response = self.client.post(f'/api/inference/documents/{self.doc.id}/share/')
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


# ---------------------------------------------------------------------------
# 10. is_shared drifted from sharing_mode
# ---------------------------------------------------------------------------

class LegacyIsSharedMirrorTests(TestCase):
    """`is_shared` is documented as superseded but is still read by old
    clients; it is now derived in `save()` rather than set by each writer."""

    def setUp(self):
        self.user = User.objects.create_user(username='mirror', password='pw')

    def _doc(self, **kwargs) -> Document:
        return Document.objects.create(
            user=self.user, name='m.txt', file_type='txt', file_size=1, **kwargs,
        )

    def test_private_document_is_not_marked_shared(self):
        self.assertFalse(self._doc().is_shared)

    def test_setting_sharing_mode_sets_the_mirror(self):
        doc = self._doc(sharing_mode='shared_read')
        self.assertTrue(doc.is_shared)

    def test_the_mirror_survives_an_update_fields_write(self):
        doc = self._doc()
        doc.sharing_mode = 'shared_write'
        doc.save(update_fields=['sharing_mode'])

        doc.refresh_from_db()
        self.assertTrue(doc.is_shared)


# ---------------------------------------------------------------------------
# 11. The posting scan cap dropped arbitrary rows, silently
# ---------------------------------------------------------------------------

class PostingScanIsOrderedAndHonestTests(TransactionTestCase):
    """
    `_scan` sliced an unordered queryset, so which postings survived the cap
    was the database's choice and could differ between identical calls — and
    nothing in the response said the ranking was over a partial scan.

    TransactionTestCase for the same reason `test_fulltext_backend` uses it:
    the backend's ORM work goes through `sync_to_async`, i.e. a second
    connection, which under plain TestCase deadlocks on the SQLite write lock
    the enclosing transaction holds.
    """

    def setUp(self):
        self.user = User.objects.create_user(username='scanner', password='pw')
        self.kb = KnowledgeBase.objects.create(
            user=self.user, name='FT', backend=KnowledgeBase.BACKEND_FULLTEXT,
        )

    def test_scan_is_ordered_by_term_frequency(self):
        from inference.backends.fulltext import FullTextBackend
        from inference.models import DocumentChunk, IndexedTerm

        doc = Document.objects.create(
            user=self.user, name='d.txt', file_type='txt', file_size=1,
            content_text='x', knowledge_base=self.kb,
        )
        for i, tf in enumerate([1, 9, 5]):
            chunk = DocumentChunk.objects.create(document=doc, chunk_index=i, content='alpha')
            IndexedTerm.objects.create(
                kb=self.kb, document=doc, chunk=chunk, term='alpha', term_frequency=tf,
            )

        rows = FullTextBackend(self.kb)._scan(['alpha'])
        frequencies = [r[3] for r in rows]
        self.assertEqual(
            frequencies, sorted(frequencies, reverse=True),
            'The cap must keep the strongest postings, not whatever the DB returned first.',
        )

    def test_a_truncated_scan_says_so(self):
        from inference.backends import fulltext
        from inference.backends.fulltext import FullTextBackend
        from inference.models import DocumentChunk, IndexedTerm

        doc = Document.objects.create(
            user=self.user, name='d.txt', file_type='txt', file_size=1,
            content_text='x', knowledge_base=self.kb,
        )
        for i in range(3):
            chunk = DocumentChunk.objects.create(
                document=doc, chunk_index=i, content=f'alpha chunk {i}',
            )
            IndexedTerm.objects.create(
                kb=self.kb, document=doc, chunk=chunk, term='alpha', term_frequency=1,
            )

        with patch.object(fulltext, 'POSTING_SCAN_LIMIT', 2):
            results = _run(FullTextBackend(self.kb).search('alpha', top_k=5))

        self.assertTrue(results)
        self.assertTrue(
            results[-1].metadata.get('scan_truncated'),
            'A partial scan and a complete one must not look alike.',
        )

    def test_a_complete_scan_carries_no_truncation_flag(self):
        from inference.backends.fulltext import FullTextBackend
        from inference.models import DocumentChunk, IndexedTerm

        doc = Document.objects.create(
            user=self.user, name='d.txt', file_type='txt', file_size=1,
            content_text='x', knowledge_base=self.kb,
        )
        chunk = DocumentChunk.objects.create(document=doc, chunk_index=0, content='alpha')
        IndexedTerm.objects.create(
            kb=self.kb, document=doc, chunk=chunk, term='alpha', term_frequency=1,
        )

        results = _run(FullTextBackend(self.kb).search('alpha', top_k=5))

        self.assertTrue(results)
        for r in results:
            self.assertNotIn('scan_truncated', r.metadata)


# ---------------------------------------------------------------------------
# 12. A pinned extraction model was never checked
# ---------------------------------------------------------------------------

class PinnedExtractionModelIsValidatedTests(APITestCase):
    """
    An unregistered `llm_model` was sent to NVIDIA regardless, so a typo
    surfaced as an opaque provider error once per document across the batch.
    The runtime fallback is kept (the registry can lag a catalogue) but the
    field is now checked where a human sets it.
    """

    def setUp(self):
        self.user = User.objects.create_user(username='pinner', password='pw12345')
        self.client.force_authenticate(user=self.user)

    def test_unknown_model_is_rejected_at_save_time(self):
        response = self.client.post('/api/extraction/schemas/', {
            'name': 'Typo', 'fields': [{'name': 'a', 'type': 'string'}],
            'llm_model': 'nvidia/nemotron-nano-12b-v2-vlll',
        }, format='json')

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('not a known model', str(response.data))

    def test_blank_model_is_fine_and_resolves_to_the_default(self):
        from inference.models import DEFAULT_EXTRACTION_MODEL

        response = self.client.post('/api/extraction/schemas/', {
            'name': 'Default', 'fields': [{'name': 'a', 'type': 'string'}],
        }, format='json')

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        schema = ExtractionSchema.objects.get(id=response.data['id'])
        self.assertEqual(schema.effective_model, DEFAULT_EXTRACTION_MODEL)


# ---------------------------------------------------------------------------
# 13. Re-indexing bypassed the event-loop bridge written for it
# ---------------------------------------------------------------------------

class ReindexRunsOnTheSharedLoopTests(TestCase):
    """
    `migration_tasks.py` and `manage.py reindex_all` each carried their own
    copy of the sweep and drove KBs with a fresh `async_to_sync` loop per KB.
    `HNSWKnowledgeBase._lock` binds to the first loop that awaits it, so a KB
    the ASGI loop had already touched raised "bound to a different event loop"
    and was silently counted as a failure. `engine.run_kb_async` exists for
    exactly this; `inference/reindex.py` is now the only place that decides.
    """

    class _FakeKB:
        stored_version = 'stale-version'
        ntotal = 3
        index_size_bytes = 99

        def __init__(self):
            self.calls = []

        async def initialize(self):
            self.calls.append('initialize')

        async def rebuild_index(self):
            self.calls.append('rebuild')

    def test_rebuild_drives_the_kb_through_run_kb_async(self):
        from inference import reindex

        fake = self._FakeKB()
        with patch.object(reindex, 'get_hnsw_kb', lambda *a, **k: fake), \
             patch.object(reindex, '_write_stats', lambda *a: None), \
             patch.object(reindex, 'run_kb_async', wraps=reindex.run_kb_async) as bridge:
            outcome = reindex.reindex_one(7, '')

        self.assertEqual(outcome, 'rebuilt')
        self.assertEqual(fake.calls, ['initialize', 'rebuild'])
        bridge.assert_called_once()

    def test_a_kb_already_on_the_current_version_is_skipped(self):
        from inference import reindex

        fake = self._FakeKB()
        fake.stored_version = reindex.EMBEDDER_VERSION
        with patch.object(reindex, 'get_hnsw_kb', lambda *a, **k: fake):
            self.assertEqual(reindex.reindex_one(7, ''), 'skipped')
        self.assertNotIn('rebuild', fake.calls)

    def test_force_rebuilds_even_when_current(self):
        from inference import reindex

        fake = self._FakeKB()
        fake.stored_version = reindex.EMBEDDER_VERSION
        with patch.object(reindex, 'get_hnsw_kb', lambda *a, **k: fake), \
             patch.object(reindex, '_write_stats', lambda *a: None):
            self.assertEqual(reindex.reindex_one(7, '', force=True), 'rebuilt')

    def test_one_bad_kb_is_counted_not_fatal_to_the_sweep(self):
        from inference import reindex

        class _Boom(self._FakeKB):
            async def initialize(self):
                raise RuntimeError('no embedder')

        with patch.object(reindex, 'get_hnsw_kb', lambda *a, **k: _Boom()):
            report = reindex.reindex_many([(1, 'a', ''), (2, 'b', '')])

        self.assertEqual(report.failed, 2)
        self.assertEqual(report.rebuilt, 0)
        self.assertEqual(len(report.failures), 2)

    def test_stats_record_the_embedder_the_index_now_speaks(self):
        from inference import reindex

        user = User.objects.create_user(username='reindexer', password='pw')
        kb = KnowledgeBase.objects.create(user=user, name='R')

        reindex._write_stats(kb.id, self._FakeKB())

        kb.refresh_from_db()
        model_name, _, dim = reindex.EMBEDDER_VERSION.partition(':')
        self.assertEqual(kb.embedding_model, model_name)
        self.assertEqual(kb.vector_dim, int(dim))
        self.assertEqual(kb.vector_count, 3)
        self.assertEqual(kb.index_size_bytes, 99)


# ---------------------------------------------------------------------------
# 14. doc_count drifted on every delete path except the API's
# ---------------------------------------------------------------------------

class DocCountIsTrueOnEveryDeletePathTests(TestCase):
    """
    Second pass at finding #4. Fixing `document_detail`'s DELETE was the wrong
    altitude: `chat/sources/attachments.py` deletes Documents from two more
    paths (`purge_rag_document`, `purge_session`), so the count still drifted —
    and `chat/tools/knowledge.py` reports it to the agent as fact. The recount
    now hangs off `post_delete`, which Django sends per instance even for a
    queryset delete, so every path is covered without enumerating them.
    """

    def setUp(self):
        self.user = User.objects.create_user(username='counted', password='pw')
        self.kb = KnowledgeBase.objects.create(
            user=self.user, name='Counted', backend=KnowledgeBase.BACKEND_RAW,
        )

    def _docs(self, n: int) -> list:
        docs = [
            Document.objects.create(
                user=self.user, name=f'{i}.txt', file_type='txt', file_size=1,
                content_text='x', knowledge_base=self.kb, status='stored',
            )
            for i in range(n)
        ]
        KnowledgeBase.objects.filter(id=self.kb.id).update(doc_count=n)
        return docs

    def _count(self) -> int:
        return KnowledgeBase.objects.get(id=self.kb.id).doc_count

    def test_instance_delete_recounts(self):
        docs = self._docs(3)
        docs[0].delete()
        self.assertEqual(self._count(), 2)

    def test_queryset_delete_recounts(self):
        docs = self._docs(3)
        # The shape `purge_session` uses — post_delete still fires per row.
        Document.objects.filter(id__in=[d.id for d in docs[:2]]).delete()
        self.assertEqual(self._count(), 1)

    def test_the_chat_attachment_purge_path_recounts(self):
        from chat.sources.attachments import purge_rag_document

        docs = self._docs(2)
        purge_rag_document('some-session', docs[0].id)

        self.assertEqual(self._count(), 1)
        self.assertFalse(Document.objects.filter(id=docs[0].id).exists())

    def test_a_document_in_no_kb_is_harmless(self):
        loose = Document.objects.create(
            user=self.user, name='loose.txt', file_type='txt', file_size=1,
        )
        self._docs(1)
        loose.delete()
        self.assertEqual(self._count(), 1)

    def test_deleting_the_kb_leaves_its_documents_and_does_not_raise(self):
        docs = self._docs(2)

        # `Document.knowledge_base` is SET_NULL, so the documents outlive their
        # KB with a null FK — they do not cascade.
        self.kb.delete()

        for doc in docs:
            doc.refresh_from_db()
            self.assertIsNone(doc.knowledge_base_id)

    def test_deleting_a_document_whose_kb_is_already_gone_is_harmless(self):
        docs = self._docs(1)
        kb_id = self.kb.id
        self.kb.delete()

        # The receiver looks up a KB that no longer exists — normal during a
        # cascade, and it must not turn a successful delete into a failure.
        docs[0].delete()

        self.assertFalse(Document.objects.filter(id=docs[0].id).exists())
        self.assertFalse(KnowledgeBase.objects.filter(id=kb_id).exists())


class MediaDocumentFileTypeTests(TestCase):
    """
    Fourth `file_type` producer, missed in the first pass: `chat/turn/pipeline`
    passed `message.message_type` — a message-*intent* vocabulary ('chat',
    'search', 'coding', 'workflow_suggestion', …) that is not Document's, and
    is `max_length=30` against Document's 10.
    """

    def test_generated_media_filenames_normalise_to_declared_choices(self):
        declared = {value for value, _label in Document.FILE_TYPE_CHOICES}
        for filename in ('generated_ab12cd34.png', 'generated_ab12cd34.mp4'):
            with self.subTest(filename=filename):
                self.assertIn(normalize_file_type(filename), declared)

    def test_message_intents_are_not_valid_file_types(self):
        # Guards the regression directly: if someone reverts to message_type,
        # these are the values that would land in the column.
        declared = {value for value, _label in Document.FILE_TYPE_CHOICES}
        for intent in ('chat', 'search', 'coding', 'workflow_suggestion'):
            with self.subTest(intent=intent):
                self.assertNotIn(intent, declared)


# ---------------------------------------------------------------------------
# 15. The dead `folder` CharField, and the promise that a move is not a re-index
# ---------------------------------------------------------------------------

class FolderFieldIsARealRelationTests(TestCase):
    """`Document.folder` spent its whole life as a CharField nothing wrote."""

    def test_folder_is_a_foreign_key_now(self):
        from django.db import models as django_models

        field = Document._meta.get_field('folder')
        self.assertIsInstance(field, django_models.ForeignKey)
        self.assertEqual(field.related_model.__name__, 'Folder')

    def test_deleting_a_folder_never_deletes_the_files_in_it(self):
        """SET_NULL, emphatically not CASCADE — a cascade would take documents
        out through the ORM collector, which never runs the vector fan-out."""
        from django.db.models import SET_NULL

        self.assertIs(Document._meta.get_field('folder').remote_field.on_delete, SET_NULL)


class MovingADocumentIsNotAReindexTests(TestCase):
    """Folders organise, KBs index. The two must not be wired together.

    Asserted on the observable state rather than by reading imports, because
    the point is what a user's document goes through, not what a module
    happens to import today. `ChokePointTests` covers the import side.
    """

    def setUp(self):
        self.user = User.objects.create_user(username='mover', password='pw')
        self.kb = KnowledgeBase.objects.create(
            user=self.user, name='Raw', backend=KnowledgeBase.BACKEND_RAW,
        )

    def test_a_move_touches_no_indexing_state(self):
        from inference import filesystem as fs
        from inference.models import DocumentChunk

        source = fs.create_folder(self.user, 'Source', None)
        target = fs.create_folder(self.user, 'Target', None)
        doc = Document.objects.create(
            user=self.user, name='a.txt', file_type='txt', file_size=1,
            content_text='hello', knowledge_base=self.kb, folder=source,
            status='indexed', chunk_count=7, indexed_at=timezone.now(),
        )
        DocumentChunk.objects.create(document=doc, chunk_index=0, content='hello')

        before = Document.objects.filter(pk=doc.pk).values(
            'status', 'chunk_count', 'indexed_at', 'knowledge_base_id',
        ).first()
        chunks_before = list(
            DocumentChunk.objects.filter(document=doc).values_list('pk', flat=True))

        fs.move(self.user, documents=[doc], target=target)

        after = Document.objects.filter(pk=doc.pk).values(
            'status', 'chunk_count', 'indexed_at', 'knowledge_base_id',
        ).first()
        self.assertEqual(before, after, 'A move is a column write, nothing more.')
        self.assertEqual(
            list(DocumentChunk.objects.filter(document=doc).values_list('pk', flat=True)),
            chunks_before,
        )
        doc.refresh_from_db()
        self.assertEqual(doc.folder_id, target.pk)
