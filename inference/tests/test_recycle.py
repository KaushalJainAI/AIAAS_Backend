"""
The recycle bin: what a delete does now, and what the 30-day sweep does later.

Two invariants carry most of the weight here and neither is enforced by a type:

*A trashed row is invisible but not gone.* Enforced by `LiveManager` being the
default manager, which is why no listing in the codebase had to be edited — and
why a test has to prove it, since nothing in the code says so at the call sites.

*The purge takes documents before folders.* This was going to be enforced by a
PROTECT foreign key; it could not be (see migration 0014), so it is enforced by
`run_recycle_sweep`'s ordering and pinned by `test_sweep_purges_documents_before_folders`.
"""
from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase, TransactionTestCase
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase

from inference import filesystem as fs
from inference import recycle
from inference.models import Document, DocumentChunk, Folder, IndexedTerm, KnowledgeBase


class BinTestCase(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='binowner', password='pw12345')
        self.client.force_authenticate(user=self.user)
        # `raw` keeps FAISS and the embedder out of the fan-out.
        self.kb = KnowledgeBase.objects.create(
            user=self.user, name='Raw', backend=KnowledgeBase.BACKEND_RAW,
        )
        self.folder = fs.create_folder(self.user, 'Reports', None)

    def _doc(self, name='a.txt', folder=None) -> Document:
        return Document.objects.create(
            user=self.user, name=name, file_type='txt', file_size=1,
            content_text='hello', knowledge_base=self.kb, folder=folder,
            status='stored',
        )

    def _age(self, days: int):
        """Backdate everything in the bin so the sweep considers it due."""
        when = timezone.now() - timedelta(days=days)
        Document.all_objects.filter(deleted_at__isnull=False).update(deleted_at=when)
        Folder.all_objects.filter(deleted_at__isnull=False).update(deleted_at=when)


class TrashHidesButKeepsTests(BinTestCase):

    def test_a_trashed_document_leaves_every_listing(self):
        doc = self._doc()
        recycle.trash(self.user, documents=[doc])

        listing = self.client.get('/api/inference/documents/')
        self.assertEqual(listing.data['my_documents'], [])
        self.assertFalse(Document.objects.filter(pk=doc.pk).exists())
        self.assertTrue(Document.all_objects.filter(pk=doc.pk).exists())

    def test_trashing_a_folder_hides_its_whole_subtree(self):
        child = fs.create_folder(self.user, 'Child', self.folder)
        grandchild = fs.create_folder(self.user, 'Grandchild', child)
        doc = self._doc(folder=grandchild)

        recycle.trash(self.user, folders=[self.folder])

        for row in (self.folder, child, grandchild):
            self.assertFalse(Folder.objects.filter(pk=row.pk).exists())
        self.assertFalse(Document.objects.filter(pk=doc.pk).exists())

    def test_only_the_deleted_node_is_listed_in_the_bin(self):
        """A subtree delete is one entry, not one per row."""
        child = fs.create_folder(self.user, 'Child', self.folder)
        fs.create_folder(self.user, 'Grandchild', child)

        recycle.trash(self.user, folders=[self.folder])
        response = self.client.get('/api/inference/trash/')

        self.assertEqual([f['id'] for f in response.data['folders']], [self.folder.pk])

    def test_the_bin_says_how_long_things_last(self):
        recycle.trash(self.user, documents=[self._doc()])
        response = self.client.get('/api/inference/trash/')

        self.assertEqual(response.data['purges_after_days'], recycle.retention_days())
        self.assertIsNotNone(response.data['documents'][0]['purges_at'])

    def test_trashing_keeps_the_content_that_restore_needs(self):
        doc = self._doc()
        recycle.trash(self.user, documents=[doc])

        doc.refresh_from_db()
        self.assertEqual(doc.content_text, 'hello')

    def test_a_trashed_name_does_not_block_reusing_it(self):
        recycle.trash(self.user, folders=[self.folder])
        # The unique constraints carry `deleted_at__isnull=True` for this.
        self.assertTrue(fs.create_folder(self.user, 'Reports', None).pk)


class TrashDropsTheIndexTests(TransactionTestCase):
    """A file the user can no longer see must stop answering RAG queries.

    TransactionTestCase because the fulltext backend does its ORM work through
    `sync_to_async`, i.e. a second connection, which deadlocks under a plain
    TestCase on SQLite.
    """

    def setUp(self):
        self.user = User.objects.create_user(username='deindex', password='pw')
        self.kb = KnowledgeBase.objects.create(
            user=self.user, name='FT', backend=KnowledgeBase.BACKEND_FULLTEXT,
        )

    def test_chunks_and_postings_go_when_a_document_is_trashed(self):
        import asyncio

        from inference.backends.fulltext import FullTextBackend

        doc = Document.objects.create(
            user=self.user, name='invoice.txt', file_type='txt', file_size=1,
            content_text='alpha beta gamma', knowledge_base=self.kb,
        )
        asyncio.run(FullTextBackend(self.kb).ingest(doc))
        self.assertTrue(DocumentChunk.objects.filter(document=doc).exists())
        self.assertTrue(IndexedTerm.objects.filter(document=doc).exists())

        recycle.trash(self.user, documents=[doc])

        self.assertFalse(
            DocumentChunk.objects.filter(document=doc).exists(),
            'A trashed document must not keep answering keyword search.',
        )
        self.assertFalse(IndexedTerm.objects.filter(document=doc).exists())

    def test_a_backend_failure_does_not_block_the_delete(self):
        doc = Document.objects.create(
            user=self.user, name='a.txt', file_type='txt', file_size=1,
            content_text='x', knowledge_base=self.kb,
        )

        async def _boom(kb, doc_id):
            raise RuntimeError('index unavailable')

        with patch('inference.tasks.remove_document_from_kb', _boom):
            recycle.trash(self.user, documents=[doc])

        # The user's delete succeeded; the sweep retries at purge time.
        self.assertFalse(Document.objects.filter(pk=doc.pk).exists())


class TrashKeepsCountsHonestTests(BinTestCase):

    def test_trashing_recounts_doc_count(self):
        """`post_delete` does not fire on a trash — the trap the extracted
        `signals.recount_kb` exists to close."""
        a, b = self._doc('a.txt'), self._doc('b.txt')
        KnowledgeBase.objects.filter(pk=self.kb.pk).update(doc_count=2)

        recycle.trash(self.user, documents=[a])

        self.kb.refresh_from_db()
        self.assertEqual(self.kb.doc_count, 1)
        self.assertTrue(Document.objects.filter(pk=b.pk).exists())


class RestoreTests(BinTestCase):

    def test_a_document_comes_back_where_it_was(self):
        doc = self._doc(folder=self.folder)
        recycle.trash(self.user, documents=[doc])

        response = self.client.post('/api/inference/trash/restore/', {
            'document_ids': [doc.pk],
        }, format='json')

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        doc.refresh_from_db()
        self.assertIsNone(doc.deleted_at)
        self.assertEqual(doc.folder_id, self.folder.pk)

    def test_restoring_a_folder_brings_its_subtree_with_it(self):
        child = fs.create_folder(self.user, 'Child', self.folder)
        doc = self._doc(folder=child)
        recycle.trash(self.user, folders=[self.folder])

        self.client.post('/api/inference/trash/restore/', {
            'folder_ids': [self.folder.pk],
        }, format='json')

        self.assertTrue(Folder.objects.filter(pk=child.pk).exists())
        self.assertTrue(Document.objects.filter(pk=doc.pk).exists())

    def test_restore_is_refused_while_the_parent_is_still_trashed(self):
        child = fs.create_folder(self.user, 'Child', self.folder)
        recycle.trash(self.user, folders=[self.folder])

        response = self.client.post('/api/inference/trash/restore/', {
            'folder_ids': [child.pk],
        }, format='json')

        self.assertEqual(response.data['restored'], [])
        self.assertEqual(response.data['refused'][0]['reason'], 'parent_still_trashed')

    def test_a_subtree_is_never_left_half_purged(self):
        """Why `restore` never has to relocate anything in practice.

        `_restore_folder` can relocate a row to root when its recorded parent
        has been purged, and that branch is kept as a defence — but the API
        cannot currently produce the state. `Folder.parent` is CASCADE so a
        child cannot outlive its parent; the sweep refuses to purge a folder
        that still holds one; and restore refuses while an ancestor is still
        trashed. Between them a trashed subtree is always wholly present or
        wholly gone. If any of those three change, this test fails and the
        relocation branch stops being merely defensive.
        """
        child = fs.create_folder(self.user, 'Child', self.folder)
        grandchild = fs.create_folder(self.user, 'Grandchild', child)
        recycle.trash(self.user, folders=[self.folder])
        self._age(60)
        # The deepest row is still inside its retention.
        Folder.all_objects.filter(pk=grandchild.pk).update(deleted_at=timezone.now())

        recycle.run_recycle_sweep()

        surviving = set(
            Folder.all_objects.filter(user=self.user).values_list('pk', flat=True))
        self.assertEqual(
            surviving, {self.folder.pk, child.pk, grandchild.pk},
            'No ancestor may be purged out from under a row that is still due '
            'to be restorable.',
        )

    def test_a_name_taken_since_the_delete_auto_suffixes(self):
        recycle.trash(self.user, folders=[self.folder])
        fs.create_folder(self.user, 'Reports', None)   # the name is taken now

        response = self.client.post('/api/inference/trash/restore/', {
            'folder_ids': [self.folder.pk],
        }, format='json')

        outcome = response.data['restored'][0]
        self.assertEqual(outcome['renamed_to'], 'Reports (2)')
        self.folder.refresh_from_db()
        self.assertEqual(self.folder.name, 'Reports (2)')

    def test_restoring_a_document_puts_it_back_in_its_index(self):
        doc = self._doc()
        recycle.trash(self.user, documents=[doc])

        started = []
        with patch('threading.Thread') as thread:
            thread.side_effect = lambda **kw: started.append(kw) or _NullThread()
            self.client.post('/api/inference/trash/restore/', {
                'document_ids': [doc.pk],
            }, format='json')

        self.assertTrue(started, 'A restored document must be re-ingested.')


class _NullThread:
    def start(self):
        pass


class SweepTests(BinTestCase):

    def test_nothing_inside_the_retention_is_touched(self):
        doc = self._doc()
        recycle.trash(self.user, documents=[doc])

        stats = recycle.run_recycle_sweep()

        self.assertEqual(stats['purged_documents'], 0)
        self.assertTrue(Document.all_objects.filter(pk=doc.pk).exists())

    def test_past_the_retention_it_goes_for_good(self):
        doc = self._doc(folder=self.folder)
        recycle.trash(self.user, folders=[self.folder])
        self._age(31)

        stats = recycle.run_recycle_sweep()

        self.assertEqual(stats['purged_documents'], 1)
        self.assertEqual(stats['purged_folders'], 1)
        self.assertFalse(Document.all_objects.filter(pk=doc.pk).exists())
        self.assertFalse(Folder.all_objects.filter(pk=self.folder.pk).exists())

    def test_documents_are_purged_before_folders(self):
        """The ordering PROTECT was meant to enforce. It could not be used
        (migration 0014), so this test is what holds the line."""
        doc = self._doc(folder=self.folder)
        recycle.trash(self.user, folders=[self.folder])
        self._age(31)

        order = []
        real_doc_delete, real_folder_delete = Document.delete, Folder.delete

        def _doc_delete(self_, *a, **kw):
            order.append('document')
            return real_doc_delete(self_, *a, **kw)

        def _folder_delete(self_, *a, **kw):
            order.append('folder')
            return real_folder_delete(self_, *a, **kw)

        with patch.object(Document, 'delete', _doc_delete), \
             patch.object(Folder, 'delete', _folder_delete):
            recycle.run_recycle_sweep()

        self.assertEqual(order, ['document', 'folder'])

    def test_folders_are_purged_deepest_first(self):
        child = fs.create_folder(self.user, 'Child', self.folder)
        grandchild = fs.create_folder(self.user, 'Grandchild', child)
        recycle.trash(self.user, folders=[self.folder])
        self._age(31)

        depths = []
        real_delete = Folder.delete
        with patch.object(Folder, 'delete',
                          lambda s, *a, **k: (depths.append(s.depth), real_delete(s, *a, **k))[1]):
            recycle.run_recycle_sweep()

        self.assertEqual(depths, sorted(depths, reverse=True))
        self.assertFalse(Folder.all_objects.filter(pk=grandchild.pk).exists())

    def test_a_parent_is_not_purged_while_it_still_holds_a_child(self):
        """`Folder.parent` is CASCADE, so purging a due parent would take a
        not-yet-due child with it — destroying something the user could still
        have restored. The sweep skips it and waits."""
        child = fs.create_folder(self.user, 'Child', self.folder)
        recycle.trash(self.user, folders=[self.folder])
        self._age(60)
        Folder.all_objects.filter(pk=child.pk).update(deleted_at=timezone.now())

        stats = recycle.run_recycle_sweep()

        self.assertEqual(stats['purged_folders'], 0)
        self.assertTrue(Folder.all_objects.filter(pk=child.pk).exists())
        self.assertTrue(Folder.all_objects.filter(pk=self.folder.pk).exists())

    def test_a_folder_is_not_purged_while_it_still_holds_a_document(self):
        """SET_NULL would spill the document out to the root instead."""
        doc = self._doc(folder=self.folder)
        recycle.trash(self.user, folders=[self.folder])
        self._age(60)
        Document.all_objects.filter(pk=doc.pk).update(deleted_at=timezone.now())

        stats = recycle.run_recycle_sweep()

        self.assertEqual(stats['purged_folders'], 0)
        self.assertTrue(Document.all_objects.filter(pk=doc.pk).exists())

    def test_the_sweep_is_idempotent(self):
        self._doc(folder=self.folder)
        recycle.trash(self.user, folders=[self.folder])
        self._age(31)

        recycle.run_recycle_sweep()
        second = recycle.run_recycle_sweep()

        self.assertEqual(second['purged_documents'], 0)
        self.assertEqual(second['failed'], 0)

    def test_a_missing_file_does_not_stall_the_sweep(self):
        doc = self._doc()
        doc.file.name = 'users/999/gone.txt'
        doc.save(update_fields=['file'])
        recycle.trash(self.user, documents=[doc])
        self._age(31)

        stats = recycle.run_recycle_sweep()

        self.assertEqual(stats['purged_documents'], 1)
        self.assertEqual(stats['failed'], 0)

    def test_live_rows_are_never_swept(self):
        live = self._doc('live.txt', folder=self.folder)
        self._age(31)          # nothing is trashed, so nothing is due

        recycle.run_recycle_sweep()

        self.assertTrue(Document.objects.filter(pk=live.pk).exists())
        self.assertTrue(Folder.objects.filter(pk=self.folder.pk).exists())

    def test_emptying_the_bin_purges_now(self):
        doc = self._doc()
        recycle.trash(self.user, documents=[doc])

        response = self.client.delete('/api/inference/trash/empty/')

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertFalse(Document.all_objects.filter(pk=doc.pk).exists())


class SweepEntryPointTests(TestCase):
    """Both doors, because a beat-only sweep fails by silently never firing."""

    def setUp(self):
        self.user = User.objects.create_user(username='cmd', password='pw')
        self.doc = Document.objects.create(
            user=self.user, name='a.txt', file_type='txt', file_size=1,
            content_text='x',
        )
        recycle.trash(self.user, documents=[self.doc])
        Document.all_objects.filter(pk=self.doc.pk).update(
            deleted_at=timezone.now() - timedelta(days=90))

    def test_the_management_command_purges(self):
        from django.core.management import call_command

        call_command('purge_recycle_bin', verbosity=0)

        self.assertFalse(Document.all_objects.filter(pk=self.doc.pk).exists())

    def test_dry_run_changes_nothing(self):
        from django.core.management import call_command

        call_command('purge_recycle_bin', '--dry-run', verbosity=0)

        self.assertTrue(Document.all_objects.filter(pk=self.doc.pk).exists())

    def test_the_celery_task_is_a_thin_wrapper(self):
        from inference.tasks import sweep_recycle_bin

        stats = sweep_recycle_bin()

        self.assertEqual(stats['purged_documents'], 1)
