"""
The per-user file system: isolation first, then tree mechanics.

The requirement this app was given is "the user will only be able to access the
files and folder in its directory only". That is not a feature you test once —
a tree multiplies the ways one id can reach another user's data, so the first
class here walks every one of them and asserts the same answer: 404, with a
body indistinguishable from an id that never existed.

Every one of those routes resolves through a single function,
`inference.filesystem.resolve_folder`. `ChokePointTests` is what stops that
becoming untrue later.
"""
from __future__ import annotations

from django.contrib.auth.models import User
from django.test import TestCase
from rest_framework import status
from rest_framework.test import APITestCase

from inference import filesystem as fs
from inference.models import Document, Folder
from workflow_backend.thresholds import MAX_FOLDER_DEPTH


class TwoUsers(APITestCase):
    """A tree of the caller's own, and a tree belonging to somebody else."""

    def setUp(self):
        self.user = User.objects.create_user(username='owner', password='pw12345')
        self.other = User.objects.create_user(username='stranger', password='pw12345')
        self.client.force_authenticate(user=self.user)

        self.mine = fs.create_folder(self.user, 'Mine', None)
        self.theirs = fs.create_folder(self.other, 'Theirs', None)
        self.their_doc = Document.objects.create(
            user=self.other, name='secret.txt', file_type='txt', file_size=1,
            content_text='secret', folder=self.theirs,
        )

    def _doc(self, name='mine.txt', folder=None) -> Document:
        return Document.objects.create(
            user=self.user, name=name, file_type='txt', file_size=1,
            content_text='hello', folder=folder,
        )


# ---------------------------------------------------------------------------
# Isolation — one test per way in
# ---------------------------------------------------------------------------

class ForeignFolderIsUnreachableTests(TwoUsers):

    def test_creating_under_a_foreign_parent_is_404(self):
        response = self.client.post('/api/inference/folders/', {
            'name': 'Sneaky', 'parent_id': self.theirs.id,
        }, format='json')

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        self.assertFalse(Folder.objects.filter(name='Sneaky').exists())

    def test_moving_into_a_foreign_folder_is_404(self):
        response = self.client.post('/api/inference/fs/move/', {
            'folder_ids': [self.mine.id], 'target_folder_id': self.theirs.id,
        }, format='json')

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        self.mine.refresh_from_db()
        self.assertIsNone(self.mine.parent_id)

    def test_moving_a_foreign_folder_is_404(self):
        response = self.client.post('/api/inference/fs/move/', {
            'folder_ids': [self.theirs.id], 'target_folder_id': self.mine.id,
        }, format='json')

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        self.theirs.refresh_from_db()
        self.assertIsNone(self.theirs.parent_id)

    def test_moving_a_foreign_document_is_404(self):
        response = self.client.post('/api/inference/fs/move/', {
            'document_ids': [self.their_doc.id], 'target_folder_id': self.mine.id,
        }, format='json')

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        self.their_doc.refresh_from_db()
        self.assertEqual(self.their_doc.folder_id, self.theirs.id)

    def test_uploading_into_a_foreign_folder_is_404(self):
        """The upload path resolves `folder_id` after the file checks, so this
        needs a real file to reach the guard at all."""
        from django.core.files.uploadedfile import SimpleUploadedFile

        upload = SimpleUploadedFile('note.txt', b'hello there', content_type='text/plain')
        response = self.client.post('/api/inference/documents/', {
            'file': upload, 'folder_id': self.theirs.id,
        }, format='multipart')

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        self.assertFalse(Document.objects.filter(name='note.txt').exists())

    def test_uploading_into_an_owned_folder_files_it_there(self):
        from django.core.files.uploadedfile import SimpleUploadedFile

        upload = SimpleUploadedFile('note.txt', b'hello there', content_type='text/plain')
        response = self.client.post('/api/inference/documents/', {
            'file': upload, 'folder_id': self.mine.id,
        }, format='multipart')

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data['folder_id'], self.mine.id)

    def test_listing_a_foreign_folder_is_404(self):
        response = self.client.get(f'/api/inference/folders/?parent={self.theirs.id}')
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_reading_a_foreign_folder_is_404(self):
        response = self.client.get(f'/api/inference/folders/{self.theirs.id}/')
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_renaming_a_foreign_folder_is_404(self):
        response = self.client.patch(
            f'/api/inference/folders/{self.theirs.id}/', {'name': 'Mine now'},
            format='json',
        )
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        self.theirs.refresh_from_db()
        self.assertEqual(self.theirs.name, 'Theirs')

    def test_deleting_a_foreign_folder_is_404(self):
        response = self.client.delete(f'/api/inference/folders/{self.theirs.id}/')
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        self.theirs.refresh_from_db()
        self.assertIsNone(self.theirs.deleted_at)

    def test_filtering_documents_by_a_foreign_folder_is_404(self):
        response = self.client.get(
            f'/api/inference/documents/?folder_id={self.theirs.id}')
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_restoring_a_foreign_row_restores_nothing(self):
        fs_folder = self.theirs
        from inference import recycle
        recycle.trash(self.other, folders=[fs_folder])

        response = self.client.post('/api/inference/trash/restore/', {
            'folder_ids': [fs_folder.id],
        }, format='json')

        # Not an error — simply nothing of the caller's matched, so nothing
        # came back. The row stays in its owner's bin.
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['restored'], [])
        fs_folder.refresh_from_db()
        self.assertIsNotNone(fs_folder.deleted_at)

    def test_trash_lists_only_the_callers_own_rows(self):
        from inference import recycle
        recycle.trash(self.other, folders=[self.theirs])
        recycle.trash(self.user, folders=[self.mine])

        response = self.client.get('/api/inference/trash/')

        ids = [f['id'] for f in response.data['folders']]
        self.assertEqual(ids, [self.mine.id])

    def test_emptying_the_bin_never_touches_another_user(self):
        from inference import recycle
        recycle.trash(self.other, folders=[self.theirs])

        self.client.delete('/api/inference/trash/empty/')

        self.assertTrue(Folder.all_objects.filter(id=self.theirs.id).exists())


class OwnershipOracleTests(TwoUsers):
    """A foreign id and a nonexistent one must be indistinguishable.

    Answering 403 for "exists but not yours" and 404 for "no such row" would
    let anyone enumerate which folder ids are real — the same reason
    `/api/orchestrator/hooks/<secret>/` answers 404 for every refusal.
    """

    def test_foreign_and_nonexistent_ids_give_identical_answers(self):
        foreign = self.client.get(f'/api/inference/folders/{self.theirs.id}/')
        missing = self.client.get('/api/inference/folders/999999/')

        self.assertEqual(foreign.status_code, missing.status_code)
        self.assertEqual(
            str(foreign.data).replace(str(self.theirs.id), 'X'),
            str(missing.data).replace('999999', 'X'),
            'A foreign id must not be distinguishable from a nonexistent one.',
        )

    def test_the_same_holds_for_listing(self):
        foreign = self.client.get(f'/api/inference/folders/?parent={self.theirs.id}')
        missing = self.client.get('/api/inference/folders/?parent=999999')
        self.assertEqual(foreign.status_code, missing.status_code)


class RestoreHasNoTargetTests(TwoUsers):
    """Restore cannot be aimed. The guard is the absence of the field."""

    def test_a_supplied_target_is_ignored(self):
        from inference import recycle

        doc = self._doc(folder=self.mine)
        recycle.trash(self.user, documents=[doc])

        response = self.client.post('/api/inference/trash/restore/', {
            'document_ids': [doc.id],
            'target_folder_id': self.theirs.id,     # not part of the contract
            'folder_id': self.theirs.id,
        }, format='json')

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        doc.refresh_from_db()
        self.assertEqual(
            doc.folder_id, self.mine.id,
            'A restore goes to the row\'s own recorded parent, never to a '
            'parent the caller names.',
        )


class ModelLevelGuardTests(TestCase):
    """The last line of defence, for a code path that skips the choke point."""

    def setUp(self):
        self.user = User.objects.create_user(username='a', password='pw')
        self.other = User.objects.create_user(username='b', password='pw')

    def test_folder_rejects_a_cross_user_parent(self):
        theirs = fs.create_folder(self.other, 'Theirs', None)
        with self.assertRaises(ValueError):
            Folder.objects.create(user=self.user, parent=theirs, name='Sneaky')

    def test_document_rejects_a_cross_user_folder(self):
        theirs = fs.create_folder(self.other, 'Theirs', None)
        with self.assertRaises(ValueError):
            Document.objects.create(
                user=self.user, name='x.txt', file_type='txt', file_size=1,
                folder=theirs,
            )


class ChokePointTests(TestCase):
    """`Folder.objects` may be reached from exactly one module.

    Sixteen scattered `.filter(user=...)` calls are only as good as the newest
    developer's memory; one function is reviewable. This test is what keeps
    that property true after everyone has forgotten it was a design decision.
    """

    ALLOWED = {'filesystem.py', 'recycle.py', 'models.py', 'signals.py',
               'admin.py', 'folder_views.py'}

    def test_folder_lookups_stay_inside_the_filesystem_module(self):
        import pathlib

        import inference

        root = pathlib.Path(inference.__file__).parent
        offenders = []
        for path in root.rglob('*.py'):
            if path.name in self.ALLOWED or 'tests' in path.parts \
                    or 'migrations' in path.parts:
                continue
            source = path.read_text(encoding='utf-8')
            if 'Folder.objects' in source or 'Folder.all_objects' in source:
                offenders.append(path.name)

        self.assertEqual(
            offenders, [],
            'Folder lookups must go through inference/filesystem.py — see its '
            'module docstring. Offending files: ' + ', '.join(offenders),
        )

    def test_the_move_path_cannot_reindex(self):
        """`filesystem` must not import `tasks`.

        Folders organise, KBs index. Keeping the tree module unable to reach
        the indexing code makes "a move never re-indexes" structural rather
        than a promise in a docstring.
        """
        import pathlib

        import inference

        source = (pathlib.Path(inference.__file__).parent / 'filesystem.py') \
            .read_text(encoding='utf-8')
        for forbidden in ('from .tasks', 'from inference.tasks', 'import tasks'):
            self.assertNotIn(forbidden, source)


class DownloadPathTests(TwoUsers):

    def test_upload_to_places_the_file_under_the_owner(self):
        from inference.utils import user_document_path

        doc = self._doc()
        path = user_document_path(doc, 'report.pdf')

        self.assertTrue(path.startswith(f'users/{self.user.id}/'))
        self.assertTrue(path.endswith('.pdf'))
        self.assertNotIn('report', path, 'The stored name must not carry user input.')

    def test_a_traversing_filename_cannot_escape_the_user_directory(self):
        from inference.utils import user_document_path

        doc = self._doc()
        path = user_document_path(doc, '../../../../etc/passwd')

        self.assertTrue(path.startswith(f'users/{self.user.id}/'))
        self.assertNotIn('..', path)

    def test_download_refuses_a_file_outside_media_root(self):
        doc = self._doc()
        doc.file.name = '../../../../etc/passwd'
        doc.save(update_fields=['file'])

        response = self.client.get(f'/api/inference/documents/{doc.id}/download/')

        # Falls back to the extracted text rather than streaming the file.
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(b''.join(response.streaming_content), b'hello')


# ---------------------------------------------------------------------------
# Tree mechanics
# ---------------------------------------------------------------------------

class TreeShapeTests(TwoUsers):

    def test_root_is_null_not_a_row(self):
        response = self.client.get('/api/inference/folders/')

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIsNone(response.data['folder'])
        self.assertEqual(response.data['breadcrumbs'], [])
        self.assertEqual(
            Folder.objects.filter(user=self.user, parent__isnull=True).count(), 1,
            'Only the folder the user made — no synthetic root row.',
        )

    def test_path_is_ids_and_is_self_inclusive(self):
        child = fs.create_folder(self.user, 'Child', self.mine)
        grandchild = fs.create_folder(self.user, 'Grandchild', child)

        self.assertEqual(self.mine.path, f'/{self.mine.id}/')
        self.assertEqual(child.path, f'/{self.mine.id}/{child.id}/')
        self.assertEqual(
            grandchild.path, f'/{self.mine.id}/{child.id}/{grandchild.id}/')
        self.assertEqual(grandchild.depth, 2)

    def test_moving_rewrites_every_descendant_path(self):
        child = fs.create_folder(self.user, 'Child', self.mine)
        grandchild = fs.create_folder(self.user, 'Grandchild', child)
        target = fs.create_folder(self.user, 'Target', None)

        fs.move(self.user, folders=[child], target=target)

        child.refresh_from_db()
        grandchild.refresh_from_db()
        self.assertEqual(child.path, f'/{target.id}/{child.id}/')
        self.assertEqual(
            grandchild.path, f'/{target.id}/{child.id}/{grandchild.id}/')
        self.assertEqual(grandchild.depth, 2)

    def test_renaming_touches_no_descendant(self):
        child = fs.create_folder(self.user, 'Child', self.mine)
        before = child.path

        fs.rename_folder(self.mine, 'Renamed')

        child.refresh_from_db()
        self.assertEqual(
            child.path, before,
            'path holds ids, so a rename is one column write — that is the '
            'whole reason it is not a name path.',
        )

    def test_a_folder_cannot_be_moved_into_itself(self):
        response = self.client.post('/api/inference/fs/move/', {
            'folder_ids': [self.mine.id], 'target_folder_id': self.mine.id,
        }, format='json')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_a_folder_cannot_be_moved_into_its_own_descendant(self):
        child = fs.create_folder(self.user, 'Child', self.mine)
        grandchild = fs.create_folder(self.user, 'Grandchild', child)

        response = self.client.post('/api/inference/fs/move/', {
            'folder_ids': [self.mine.id], 'target_folder_id': grandchild.id,
        }, format='json')

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.mine.refresh_from_db()
        self.assertIsNone(self.mine.parent_id)

    def test_depth_is_capped(self):
        """Asserted by digging until it refuses, rather than by arithmetic —
        an off-by-one in the test would otherwise pass while the cap leaked."""
        parent = self.mine
        for i in range(MAX_FOLDER_DEPTH + 5):
            try:
                parent = fs.create_folder(self.user, f'L{i}', parent)
            except fs.FilesystemError:
                break
        else:
            self.fail('Nesting was never refused — the depth cap does not bite.')

        deepest = Folder.objects.filter(user=self.user).order_by('-depth').first()
        self.assertLess(deepest.depth, MAX_FOLDER_DEPTH)
        self.assertEqual(deepest.depth, MAX_FOLDER_DEPTH - 1)

    def test_a_move_cannot_smuggle_a_subtree_past_the_depth_cap(self):
        """The cap has to account for what hangs *below* the folder moved."""
        deep = self.mine
        for i in range(MAX_FOLDER_DEPTH - 3):
            deep = fs.create_folder(self.user, f'D{i}', deep)

        branch = fs.create_folder(self.user, 'Branch', None)
        leaf = fs.create_folder(self.user, 'Leaf', branch)
        fs.create_folder(self.user, 'Deeper', leaf)

        with self.assertRaises(fs.FilesystemError):
            fs.move(self.user, folders=[branch], target=deep)

    def test_duplicate_sibling_name_is_refused(self):
        fs.create_folder(self.user, 'Reports', self.mine)
        response = self.client.post('/api/inference/folders/', {
            'name': 'Reports', 'parent_id': self.mine.id,
        }, format='json')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_duplicate_top_level_name_is_refused(self):
        """The NULL-parent constraint. SQL treats NULLs as distinct, so the
        (user, parent, name) constraint does not reach root at all — this is
        the one most likely to be quietly missing."""
        response = self.client.post(
            '/api/inference/folders/', {'name': 'Mine'}, format='json')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_the_same_name_is_fine_for_two_different_users(self):
        self.assertTrue(fs.create_folder(self.other, 'Mine', None).pk)

    def test_a_name_cannot_contain_a_separator(self):
        for bad in ('a/b', 'a\\b', '..', '.'):
            with self.subTest(name=bad):
                response = self.client.post(
                    '/api/inference/folders/', {'name': bad}, format='json')
                self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_breadcrumbs_are_root_first_and_exclude_self(self):
        child = fs.create_folder(self.user, 'Child', self.mine)
        grandchild = fs.create_folder(self.user, 'Grandchild', child)

        trail = fs.breadcrumbs(grandchild)

        self.assertEqual([c['name'] for c in trail], ['Mine', 'Child'])


class DocumentPlacementTests(TwoUsers):

    def test_a_document_with_no_folder_is_at_root(self):
        doc = self._doc()
        response = self.client.get(f'/api/inference/documents/{doc.id}/')

        self.assertIsNone(response.data['folder_id'])
        self.assertEqual(response.data['folder_path'], '/')

    def test_documents_can_be_filtered_to_one_folder(self):
        here = self._doc('here.txt', folder=self.mine)
        self._doc('elsewhere.txt', folder=None)

        response = self.client.get(
            f'/api/inference/documents/?folder_id={self.mine.id}')

        names = [d['filename'] for d in response.data['my_documents']]
        self.assertEqual(names, ['here.txt'])

    def test_root_is_addressable_as_a_folder_filter(self):
        self._doc('here.txt', folder=self.mine)
        self._doc('at-root.txt', folder=None)

        response = self.client.get('/api/inference/documents/?folder_id=root')

        names = [d['filename'] for d in response.data['my_documents']]
        self.assertEqual(names, ['at-root.txt'])

    def test_omitting_folder_id_keeps_the_old_flat_listing(self):
        """The compatibility contract: existing clients pass no folder_id and
        must keep seeing everything, wherever it now sits in the tree."""
        self._doc('a.txt', folder=self.mine)
        self._doc('b.txt', folder=None)

        response = self.client.get('/api/inference/documents/')

        names = {d['filename'] for d in response.data['my_documents']}
        self.assertEqual(names, {'a.txt', 'b.txt'})

    def test_folder_path_is_the_human_readable_location(self):
        child = fs.create_folder(self.user, 'Child', self.mine)
        doc = self._doc(folder=child)

        response = self.client.get(f'/api/inference/documents/{doc.id}/')

        self.assertEqual(response.data['folder_path'], '/Mine/Child')

    def test_moving_a_document_is_a_column_write(self):
        doc = self._doc(folder=None)

        response = self.client.post('/api/inference/fs/move/', {
            'document_ids': [doc.id], 'target_folder_id': self.mine.id,
        }, format='json')

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        doc.refresh_from_db()
        self.assertEqual(doc.folder_id, self.mine.id)


class UserDeletionTests(TestCase):
    """`Document.folder` is PROTECT, which is exactly the kind of thing that
    works until somebody deletes an account."""

    def test_deleting_a_user_with_folders_and_documents_succeeds(self):
        user = User.objects.create_user(username='doomed', password='pw')
        folder = fs.create_folder(user, 'Stuff', None)
        child = fs.create_folder(user, 'More', folder)
        Document.objects.create(
            user=user, name='a.txt', file_type='txt', file_size=1, folder=child,
        )

        user.delete()

        self.assertFalse(Folder.all_objects.filter(user_id=user.id).exists())
        self.assertFalse(Document.all_objects.filter(user_id=user.id).exists())
