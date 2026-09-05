"""
Tests for the agent's virtual filesystem (`inference/vfs.py`).

Grouped by the property each one defends rather than by function, because the
failures that matter here are all silent: a traversal that quietly resolves
into somebody else's tree, a read-only agent that turns out to be able to
write, a truncated read that looks complete. None of those raise on their own.
"""
from __future__ import annotations

from django.contrib.auth import get_user_model
from django.test import TestCase

from inference import filesystem as fs
from inference import vfs
from inference.models import Document, Folder
from workflow_backend.thresholds import (
    AGENT_FILE_READ_CHARS,
    AGENT_FILE_WRITE_CHARS,
    AGENT_HOME_ROOT,
)

User = get_user_model()


class VfsTestCase(TestCase):
    def setUp(self):
        self.user = User.objects.create_user('owner', 'owner@example.com', 'pw')
        self.other = User.objects.create_user('thief', 'thief@example.com', 'pw')

    def scope(self, mode=vfs.SCOPED, user=None, name='Researcher'):
        return vfs.build_scope(user or self.user, mode, agent_name=name)


# ─────────────────────────────────────────────────────────────────────────
# Scope construction
# ─────────────────────────────────────────────────────────────────────────

class ScopeTests(VfsTestCase):
    def test_none_yields_no_scope(self):
        # Not an empty scope — no scope at all, so the caller withholds the
        # tools rather than offering ones that refuse.
        self.assertIsNone(vfs.build_scope(self.user, 'none'))

    def test_unknown_access_is_treated_as_none(self):
        self.assertIsNone(vfs.build_scope(self.user, 'sudo'))
        self.assertIsNone(vfs.build_scope(self.user, ''))

    def test_scoped_creates_a_home_under_agents(self):
        scope = self.scope()
        self.assertEqual(scope.root.name, 'Researcher')
        self.assertEqual(scope.root.parent.name, AGENT_HOME_ROOT)
        self.assertIsNone(scope.root.parent.parent)
        self.assertEqual(scope.label, f'/{AGENT_HOME_ROOT}/Researcher')

    def test_home_is_reused_not_duplicated(self):
        first, second = self.scope(), self.scope()
        self.assertEqual(first.root.pk, second.root.pk)
        self.assertEqual(Folder.objects.filter(user=self.user, name='Researcher').count(), 1)

    def test_two_agents_get_separate_homes(self):
        a = self.scope(name='Alpha')
        b = self.scope(name='Beta')
        self.assertNotEqual(a.root.pk, b.root.pk)
        self.assertEqual(a.root.parent.pk, b.root.parent.pk)

    def test_two_users_never_share_a_home(self):
        mine = self.scope()
        theirs = self.scope(user=self.other)
        self.assertNotEqual(mine.root.pk, theirs.root.pk)
        self.assertEqual(theirs.root.user_id, self.other.id)

    def test_agent_name_with_separators_is_stored_not_refused(self):
        scope = self.scope(name='ops/../admin')
        self.assertNotIn('/', scope.root.name)
        self.assertEqual(scope.root.parent.name, AGENT_HOME_ROOT)

    def test_blank_agent_name_still_gets_a_home(self):
        scope = self.scope(name='')
        self.assertEqual(scope.root.name, 'agent')

    def test_readonly_and_full_root_at_the_user_tree(self):
        for mode in (vfs.READONLY, vfs.FULL):
            scope = self.scope(mode=mode)
            self.assertIsNone(scope.root)
            self.assertEqual(scope.label, '/')

    def test_writability_by_mode(self):
        self.assertFalse(self.scope(mode=vfs.READONLY).writable)
        self.assertTrue(self.scope(mode=vfs.SCOPED).writable)
        self.assertTrue(self.scope(mode=vfs.FULL).writable)


# ─────────────────────────────────────────────────────────────────────────
# Path resolution — the property the whole design rests on
# ─────────────────────────────────────────────────────────────────────────

class PathWalkTests(VfsTestCase):
    def test_dot_dot_is_clamped_at_the_root(self):
        # The central claim: `..` cannot escape, and does not error either.
        self.assertEqual(vfs.segments('../../../etc/passwd'), ['etc', 'passwd'])
        self.assertEqual(vfs.segments('/../..'), [])
        self.assertEqual(vfs.segments('a/../../b'), ['b'])

    def test_dot_and_empty_segments_drop(self):
        self.assertEqual(vfs.segments('/a/./b//c/'), ['a', 'b', 'c'])
        self.assertEqual(vfs.segments(''), [])
        self.assertEqual(vfs.segments('/'), [])
        self.assertEqual(vfs.segments(None), [])

    def test_backslashes_are_separators_too(self):
        self.assertEqual(vfs.segments(r'notes\2026\q1.md'), ['notes', '2026', 'q1.md'])

    def test_traversal_cannot_reach_a_sibling_agent(self):
        mine = self.scope(name='Alpha')
        vfs.write_file(mine, 'secret.md', 'mine')
        other_agent = self.scope(name='Beta')
        vfs.write_file(other_agent, 'theirs.md', 'theirs')

        # From Alpha's scope, every spelling of "go up and over" lands back
        # inside Alpha.
        for attempt in ('../Beta/theirs.md', '../../Agents/Beta/theirs.md',
                        '/../Beta/theirs.md'):
            with self.assertRaises(vfs.VfsError):
                vfs.read_file(mine, attempt)

    def test_traversal_cannot_reach_another_user(self):
        theirs = self.scope(user=self.other, name='Alpha')
        vfs.write_file(theirs, 'private.md', 'classified')

        mine = self.scope(name='Alpha')
        with self.assertRaises(vfs.VfsError):
            vfs.read_file(mine, '../../Agents/Alpha/private.md')

        listing = vfs.list_dir(mine, '/')
        self.assertEqual(listing['files'], [])

    def test_full_access_still_cannot_leave_the_user(self):
        # `full` is the whole tree — of one user. It is not a global scope.
        fs.create_folder(self.other, 'Theirs', None)
        Document.objects.create(
            user=self.other, folder=None, name='theirs.md', file='',
            file_type='md', file_size=1, content_text='x', status='stored',
        )
        mine = self.scope(mode=vfs.FULL)
        listing = vfs.list_dir(mine, '/')
        self.assertEqual(listing['files'], [])
        self.assertEqual(listing['directories'], [])

    def test_missing_directory_names_the_failing_segment(self):
        scope = self.scope()
        with self.assertRaises(vfs.VfsError) as ctx:
            vfs.list_dir(scope, 'reports/2026/q1')
        self.assertIn('reports', str(ctx.exception))


# ─────────────────────────────────────────────────────────────────────────
# Reads and writes
# ─────────────────────────────────────────────────────────────────────────

class ReadWriteTests(VfsTestCase):
    def test_write_then_read_round_trips(self):
        scope = self.scope()
        written = vfs.write_file(scope, 'notes.md', '# Hello')
        self.assertTrue(written['created'])

        read = vfs.read_file(scope, 'notes.md')
        self.assertEqual(read['content'], '# Hello')
        self.assertEqual(read['document_id'], written['document_id'])

    def test_write_creates_missing_parents(self):
        scope = self.scope()
        vfs.write_file(scope, 'reports/2026/q1.md', 'body')
        self.assertEqual(vfs.read_file(scope, 'reports/2026/q1.md')['content'], 'body')
        self.assertEqual(vfs.list_dir(scope, '/')['directories'], ['reports/'])

    def test_second_write_overwrites_in_place(self):
        scope = self.scope()
        first = vfs.write_file(scope, 'n.md', 'one')
        second = vfs.write_file(scope, 'n.md', 'two')
        self.assertFalse(second['created'])
        self.assertEqual(first['document_id'], second['document_id'])
        self.assertEqual(vfs.read_file(scope, 'n.md')['content'], 'two')
        self.assertEqual(Document.objects.filter(user=self.user, name='n.md').count(), 1)

    def test_append_adds_to_the_end(self):
        scope = self.scope()
        vfs.write_file(scope, 'log.txt', 'a\n')
        vfs.write_file(scope, 'log.txt', 'b\n', append=True)
        self.assertEqual(vfs.read_file(scope, 'log.txt')['content'], 'a\nb\n')

    def test_written_files_are_stored_never_indexed(self):
        # "Folders organise, KBs index" — writing a file must not start an
        # embedding job or attach itself to a knowledge base.
        scope = self.scope()
        doc_id = vfs.write_file(scope, 'n.md', 'x')['document_id']
        doc = Document.objects.get(pk=doc_id)
        self.assertEqual(doc.status, 'stored')
        self.assertIsNone(doc.knowledge_base_id)
        self.assertEqual(doc.chunk_count, 0)
        self.assertIsNone(doc.indexed_at)

    def test_file_type_comes_from_the_extension(self):
        scope = self.scope()
        for name, expected in [('a.md', 'md'), ('b.json', 'json'), ('c.csv', 'csv'),
                               ('d.html', 'html'), ('e.weird', 'txt'), ('f', 'txt')]:
            doc_id = vfs.write_file(scope, name, 'x')['document_id']
            self.assertEqual(Document.objects.get(pk=doc_id).file_type, expected, name)

    def test_long_read_is_truncated_and_says_so(self):
        scope = self.scope()
        body = 'x' * (AGENT_FILE_READ_CHARS + 5_000)
        vfs.write_file(scope, 'big.txt', body)

        first = vfs.read_file(scope, 'big.txt')
        self.assertTrue(first['truncated'])
        self.assertEqual(first['chars'], AGENT_FILE_READ_CHARS)
        self.assertEqual(first['total_chars'], len(body))
        self.assertIn(str(AGENT_FILE_READ_CHARS), first['note'])

        rest = vfs.read_file(scope, 'big.txt', offset=AGENT_FILE_READ_CHARS)
        self.assertEqual(rest['chars'], 5_000)
        self.assertNotIn('truncated', rest)

    def test_write_over_the_cap_is_refused(self):
        scope = self.scope()
        with self.assertRaises(vfs.VfsError) as ctx:
            vfs.write_file(scope, 'huge.txt', 'x' * (AGENT_FILE_WRITE_CHARS + 1))
        self.assertIn('limit', str(ctx.exception))
        self.assertFalse(Document.objects.filter(user=self.user, name='huge.txt').exists())

    def test_append_over_the_cap_is_refused_and_leaves_the_file_intact(self):
        scope = self.scope()
        vfs.write_file(scope, 'log.txt', 'x' * (AGENT_FILE_WRITE_CHARS - 10))
        with self.assertRaises(vfs.VfsError):
            vfs.write_file(scope, 'log.txt', 'y' * 100, append=True)
        self.assertEqual(vfs.read_file(scope, 'log.txt')['total_chars'],
                         AGENT_FILE_WRITE_CHARS - 10)

    def test_reading_a_missing_file_says_what_to_do(self):
        scope = self.scope()
        with self.assertRaises(vfs.VfsError) as ctx:
            vfs.read_file(scope, 'nope.md')
        self.assertIn('No such file', str(ctx.exception))

    def test_a_path_naming_the_root_is_refused(self):
        scope = self.scope()
        with self.assertRaises(vfs.VfsError):
            vfs.read_file(scope, '/')
        with self.assertRaises(vfs.VfsError):
            vfs.write_file(scope, '/', 'x')

    def test_binary_upload_with_no_text_is_explained(self):
        # An unprocessed PDF is not an empty file, and a model told "" would
        # report the document as blank.
        scope = self.scope()
        Document.objects.create(
            user=self.user, folder=scope.root, name='scan.pdf', file='',
            file_type='pdf', file_size=100, content_text='', status='pending',
        )
        out = vfs.read_file(scope, 'scan.pdf')
        self.assertEqual(out['content'], '')
        self.assertIn('binary', out['note'])


# ─────────────────────────────────────────────────────────────────────────
# Mode enforcement
class EditTests(VfsTestCase):
    """
    `edit_file` exists because `write_file` is whole-file replacement, so the
    only way to change one line was to re-emit every other line. What is tested
    here is mostly the *refusals*: an edit that silently does nothing, or that
    lands on the wrong one of several matches, is invisible to everything
    downstream.
    """

    def test_replaces_one_run_and_leaves_the_rest(self):
        scope = self.scope()
        vfs.write_file(scope, 'notes.md', '# Q1\nRevenue rose.\nCosts fell.\n')

        out = vfs.edit_file(scope, 'notes.md', 'Revenue rose.', 'Revenue doubled.')

        self.assertEqual(out['replacements'], 1)
        self.assertEqual(
            vfs.read_file(scope, 'notes.md')['content'],
            '# Q1\nRevenue doubled.\nCosts fell.\n',
        )

    def test_it_edits_in_place_rather_than_making_a_second_file(self):
        scope = self.scope()
        written = vfs.write_file(scope, 'n.md', 'one')
        edited = vfs.edit_file(scope, 'n.md', 'one', 'two')
        self.assertEqual(written['document_id'], edited['document_id'])
        self.assertEqual(Document.objects.filter(user=self.user, name='n.md').count(), 1)

    def test_file_size_follows_the_new_text(self):
        scope = self.scope()
        doc_id = vfs.write_file(scope, 'n.md', 'short')['document_id']
        vfs.edit_file(scope, 'n.md', 'short', 'considerably longer')
        doc = Document.objects.get(pk=doc_id)
        self.assertEqual(doc.file_size, len('considerably longer'.encode('utf-8')))

    def test_text_that_is_not_there_is_an_error_not_a_silent_no_op(self):
        # The failure this refusal prevents: a model told "done" builds its
        # next three steps on a file it believes it has already changed.
        scope = self.scope()
        vfs.write_file(scope, 'n.md', 'hello')
        with self.assertRaises(vfs.VfsError) as cm:
            vfs.edit_file(scope, 'n.md', 'goodbye', 'hi')
        self.assertIn('not in the file', str(cm.exception))
        self.assertEqual(vfs.read_file(scope, 'n.md')['content'], 'hello')

    def test_an_ambiguous_match_is_refused_and_says_how_many(self):
        scope = self.scope()
        vfs.write_file(scope, 'n.md', 'todo\ntodo\ntodo\n')
        with self.assertRaises(vfs.VfsError) as cm:
            vfs.edit_file(scope, 'n.md', 'todo', 'done')
        # The count is the point: zero means re-read, several means give more
        # context, and the model cannot tell them apart from "no match".
        self.assertIn('3 times', str(cm.exception))
        self.assertEqual(vfs.read_file(scope, 'n.md')['content'], 'todo\ntodo\ntodo\n')

    def test_replace_all_takes_every_occurrence(self):
        scope = self.scope()
        vfs.write_file(scope, 'n.md', 'todo\ntodo\ntodo\n')
        out = vfs.edit_file(scope, 'n.md', 'todo', 'done', replace_all=True)
        self.assertEqual(out['replacements'], 3)
        self.assertEqual(vfs.read_file(scope, 'n.md')['content'], 'done\ndone\ndone\n')

    def test_empty_new_text_deletes_the_match(self):
        scope = self.scope()
        vfs.write_file(scope, 'n.md', 'keep DROP keep')
        vfs.edit_file(scope, 'n.md', ' DROP', '')
        self.assertEqual(vfs.read_file(scope, 'n.md')['content'], 'keep keep')

    def test_empty_old_text_is_refused(self):
        scope = self.scope()
        vfs.write_file(scope, 'n.md', 'body')
        with self.assertRaises(vfs.VfsError) as cm:
            vfs.edit_file(scope, 'n.md', '', 'x')
        self.assertIn('write_file', str(cm.exception))

    def test_editing_a_missing_file_says_what_to_do(self):
        scope = self.scope()
        with self.assertRaises(vfs.VfsError) as cm:
            vfs.edit_file(scope, 'nope.md', 'a', 'b')
        self.assertIn('No such file', str(cm.exception))

    def test_a_file_with_no_extracted_text_is_explained(self):
        scope = self.scope()
        Document.objects.create(
            user=self.user, folder=scope.root, name='scan.pdf', file='',
            file_type='pdf', file_size=10, content_text='', status='stored',
        )
        with self.assertRaises(vfs.VfsError) as cm:
            vfs.edit_file(scope, 'scan.pdf', 'a', 'b')
        self.assertIn('binary upload', str(cm.exception))

    def test_an_edit_over_the_cap_is_refused_and_leaves_the_file_intact(self):
        scope = self.scope()
        vfs.write_file(scope, 'n.md', 'seed')
        with self.assertRaises(vfs.VfsError):
            vfs.edit_file(scope, 'n.md', 'seed', 'x' * (AGENT_FILE_WRITE_CHARS + 1))
        self.assertEqual(vfs.read_file(scope, 'n.md')['content'], 'seed')

    def test_readonly_cannot_edit(self):
        # The grant/scope split: `fileOps` says it may touch files, the scope
        # says it may not write, and an edit is a write.
        writer = self.scope()
        vfs.write_file(writer, 'n.md', 'original')

        reader = self.scope(vfs.READONLY)
        with self.assertRaises(vfs.VfsError):
            vfs.edit_file(reader, 'n.md', 'original', 'changed')

    def test_read_all_write_own_cannot_edit_outside_its_home(self):
        # The one mode where readable and writable differ: an edit reaching a
        # file it can only read would make that distinction meaningless.
        other_doc = Document.objects.create(
            user=self.user, folder=None, name='user-note.md', file='',
            file_type='md', file_size=4, content_text='mine', status='stored',
        )
        scope = vfs.build_scope(self.user, vfs.READ_ALL_WRITE_OWN, agent_name='Researcher')
        self.assertIn('mine', vfs.read_file(scope, '/user-note.md')['content'])

        with self.assertRaises(vfs.VfsError):
            vfs.edit_file(scope, '/user-note.md', 'mine', 'theirs')
        other_doc.refresh_from_db()
        self.assertEqual(other_doc.content_text, 'mine')


# ─────────────────────────────────────────────────────────────────────────

class ReadOnlyTests(VfsTestCase):
    def test_readonly_cannot_write_mkdir_or_delete(self):
        scope = self.scope(mode=vfs.READONLY)
        for call in (
            lambda: vfs.write_file(scope, 'n.md', 'x'),
            lambda: vfs.make_dir(scope, 'notes'),
            lambda: vfs.delete(scope, 'n.md'),
        ):
            with self.assertRaises(vfs.VfsError) as ctx:
                call()
            self.assertIn('read-only', str(ctx.exception))

    def test_readonly_can_still_read_and_list(self):
        Document.objects.create(
            user=self.user, folder=None, name='given.md', file='',
            file_type='md', file_size=5, content_text='hello', status='stored',
        )
        scope = self.scope(mode=vfs.READONLY)
        self.assertEqual(vfs.read_file(scope, 'given.md')['content'], 'hello')
        self.assertEqual([f['name'] for f in vfs.list_dir(scope, '/')['files']],
                         ['given.md'])

    def test_a_failed_write_creates_no_folders(self):
        # The refusal has to come before `mkdir -p` runs, or a read-only agent
        # leaves directories behind in the user's tree.
        scope = self.scope(mode=vfs.READONLY)
        with self.assertRaises(vfs.VfsError):
            vfs.write_file(scope, 'a/b/c.md', 'x')
        self.assertFalse(Folder.objects.filter(user=self.user, name='a').exists())


# ─────────────────────────────────────────────────────────────────────────
# Listing and deletion
# ─────────────────────────────────────────────────────────────────────────

class ListAndDeleteTests(VfsTestCase):
    def test_listing_separates_directories_from_files(self):
        scope = self.scope()
        vfs.write_file(scope, 'a.md', 'x')
        vfs.make_dir(scope, 'sub')
        out = vfs.list_dir(scope, '/')
        self.assertEqual(out['directories'], ['sub/'])
        self.assertEqual([f['name'] for f in out['files']], ['a.md'])
        self.assertTrue(out['writable'])
        self.assertFalse(out['truncated'])

    def test_listing_is_not_recursive(self):
        scope = self.scope()
        vfs.write_file(scope, 'sub/deep.md', 'x')
        out = vfs.list_dir(scope, '/')
        self.assertEqual([f['name'] for f in out['files']], [])
        self.assertEqual(out['directories'], ['sub/'])

    def test_delete_trashes_rather_than_destroys(self):
        scope = self.scope()
        doc_id = vfs.write_file(scope, 'n.md', 'x')['document_id']
        out = vfs.delete(scope, 'n.md')

        self.assertEqual(out['deleted'], 'file')
        self.assertTrue(out['restorable'])
        # Gone from every live listing, still there to restore.
        self.assertFalse(Document.objects.filter(pk=doc_id).exists())
        self.assertTrue(Document.all_objects.filter(pk=doc_id).exists())
        self.assertEqual(vfs.list_dir(scope, '/')['files'], [])

    def test_delete_removes_a_directory_and_its_contents(self):
        scope = self.scope()
        vfs.write_file(scope, 'sub/a.md', 'x')
        vfs.delete(scope, 'sub')
        self.assertEqual(vfs.list_dir(scope, '/')['directories'], [])

    def test_delete_refuses_the_root(self):
        scope = self.scope()
        with self.assertRaises(vfs.VfsError) as ctx:
            vfs.delete(scope, '/')
        self.assertIn('Refusing', str(ctx.exception))

    def test_writing_after_deleting_the_same_name_works(self):
        # The trashed row is invisible to `Document.objects`, so the new write
        # must create rather than collide with something the model cannot see.
        scope = self.scope()
        first = vfs.write_file(scope, 'n.md', 'one')['document_id']
        vfs.delete(scope, 'n.md')
        second = vfs.write_file(scope, 'n.md', 'two')
        self.assertTrue(second['created'])
        self.assertNotEqual(first, second['document_id'])
        self.assertEqual(vfs.read_file(scope, 'n.md')['content'], 'two')

    def test_delete_of_a_missing_path_is_an_error_not_a_silent_success(self):
        scope = self.scope()
        with self.assertRaises(vfs.VfsError):
            vfs.delete(scope, 'never-existed.md')


# ─────────────────────────────────────────────────────────────────────────
# The choke point still holds
# ─────────────────────────────────────────────────────────────────────────

class ChokePointTests(TestCase):
    def test_vfs_does_not_reach_folder_objects_directly(self):
        """`vfs.py` is a path API, which is exactly the shape that would tempt
        someone to match `Folder.path` against a string. It must keep walking
        through `filesystem.py` instead — the invariant that whole module
        exists to hold."""
        import pathlib

        import inference

        source = (pathlib.Path(inference.__file__).parent / 'vfs.py').read_text(
            encoding='utf-8')
        self.assertNotIn('Folder.objects', source)
        self.assertNotIn('Folder.all_objects', source)
        self.assertNotIn('path__startswith', source)

    def test_vfs_never_touches_a_real_filesystem(self):
        """The 'artificial' half of the design. If `os` ever appears here, the
        thing being sandboxed has stopped being rows."""
        import pathlib

        import inference

        source = (pathlib.Path(inference.__file__).parent / 'vfs.py').read_text(
            encoding='utf-8')
        for forbidden in ('import os', 'import pathlib', 'open(', 'shutil'):
            self.assertNotIn(forbidden, source)


# ─────────────────────────────────────────────────────────────────────────
# End to end: the runtime, the tools, and the scope between them
# ─────────────────────────────────────────────────────────────────────────

class RuntimeWiringTests(TestCase):
    """The plumbing, not the logic. Every piece above can be right while the
    scope never reaches the tool that needs it — which is the failure mode of a
    value that is stored, round-tripped to the UI, and read by nothing."""

    def setUp(self):
        self.user = User.objects.create_user('owner', 'owner@example.com', 'pw')

    def _agent(self, **kw):
        from agents.models import SubAgent

        defaults = dict(
            name='Writer', user=self.user, prompt='write things',
            tool_grants={'fileOps': True},
            sandbox={'fileAccess': 'scoped'},
        )
        defaults.update(kw)
        return SubAgent.objects.create(**defaults)

    def test_scope_is_built_from_the_agents_settings(self):
        from asgiref.sync import async_to_sync
        from agents.agent.runtime import build_file_scope

        scope = async_to_sync(build_file_scope)(self._agent(), self.user)
        self.assertIsNotNone(scope)
        self.assertEqual(scope.mode, vfs.SCOPED)
        self.assertEqual(scope.root.name, 'Writer')

    def test_no_grant_means_no_scope_and_no_folder_is_created(self):
        from asgiref.sync import async_to_sync
        from agents.agent.runtime import build_file_scope

        agent = self._agent(tool_grants={'fileOps': False})
        self.assertIsNone(async_to_sync(build_file_scope)(agent, self.user))
        # Nothing was created on the way to deciding that.
        self.assertFalse(Folder.objects.filter(user=self.user).exists())

    def test_file_access_none_means_no_scope_even_with_the_grant(self):
        from asgiref.sync import async_to_sync
        from agents.agent.runtime import build_file_scope

        agent = self._agent(sandbox={'fileAccess': 'none'})
        self.assertIsNone(async_to_sync(build_file_scope)(agent, self.user))

    def test_tools_reach_the_scope_through_the_tool_context(self):
        from asgiref.sync import async_to_sync
        from chat.tools import execute_tool

        scope = vfs.build_scope(self.user, 'scoped', agent_name='Writer')
        context = {'user_id': self.user.id, 'file_scope': scope}

        written = async_to_sync(execute_tool)(
            'write_file', {'path': 'out/report.md', 'content': '# Done'}, context)
        self.assertIn('report.md', written)

        listed = async_to_sync(execute_tool)('list_files', {'path': 'out'}, context)
        self.assertIn('report.md', listed)

        read = async_to_sync(execute_tool)(
            'read_file', {'path': 'out/report.md'}, context)
        self.assertIn('# Done', read)

    def test_tools_decline_when_the_context_carries_no_scope(self):
        from asgiref.sync import async_to_sync
        from chat.tools import execute_tool

        out = async_to_sync(execute_tool)(
            'write_file', {'path': 'x.md', 'content': 'y'},
            {'user_id': self.user.id})
        self.assertIn('no file access', out.lower())

    def test_the_user_sees_what_the_agent_wrote(self):
        # The point of writing into the user's own tree rather than a scratch
        # dir: the file is an ordinary Document in an ordinary Folder.
        scope = vfs.build_scope(self.user, 'scoped', agent_name='Writer')
        vfs.write_file(scope, 'notes.md', 'hello')

        agents_root = Folder.objects.get(user=self.user, name=AGENT_HOME_ROOT,
                                         parent=None)
        home = Folder.objects.get(user=self.user, name='Writer',
                                  parent=agents_root)
        doc = Document.objects.get(user=self.user, folder=home, name='notes.md')
        self.assertEqual(doc.content_text, 'hello')


# ─────────────────────────────────────────────────────────────────────────
# Read-all / write-own: the one mode where the two subtrees differ
# ─────────────────────────────────────────────────────────────────────────

class ReadAllWriteOwnTests(VfsTestCase):
    """The setting that reads the whole tree and writes only its own home.

    Every other mode gets its confinement free from the walk, because the
    readable and writable subtrees are the same. This one does not, so these
    tests exist to prove the write gate does the job the walk is not doing —
    above all that widening reads did not quietly widen writes.
    """

    def setUp(self):
        super().setUp()
        self.scope_raw = vfs.build_scope(
            self.user, vfs.READ_ALL_WRITE_OWN, agent_name='Reporter')
        # A document somewhere else in the user's tree — readable, not writable.
        self.elsewhere = fs.ensure_folder(self.user, 'Finance', None)
        Document.objects.create(
            user=self.user, folder=self.elsewhere, name='q1.md', file='',
            file_type='md', file_size=len(b'revenue up'),
            content_text='revenue up', status='stored',
        )

    def test_it_roots_reads_at_the_whole_tree(self):
        self.assertIsNone(self.scope_raw.root)
        self.assertEqual(self.scope_raw.label, '/')

    def test_it_pins_writes_to_the_agents_own_home(self):
        self.assertEqual(
            self.scope_raw.write_prefix, (AGENT_HOME_ROOT, 'Reporter'))
        self.assertEqual(
            self.scope_raw.write_label, f'/{AGENT_HOME_ROOT}/Reporter')

    def test_it_can_read_a_file_outside_its_home(self):
        out = vfs.read_file(self.scope_raw, 'Finance/q1.md')

        self.assertIn('revenue up', out['content'])

    def test_the_same_path_it_can_read_it_cannot_write(self):
        """The whole point of the mode, in one assertion.

        A single test, deliberately: read-succeeds and write-fails are only
        interesting *together*. Either alone is satisfied by an existing mode.
        """
        vfs.read_file(self.scope_raw, 'Finance/q1.md')

        with self.assertRaises(vfs.VfsError) as caught:
            vfs.write_file(self.scope_raw, 'Finance/q1.md', 'tampered')

        self.assertIn('Reporter', str(caught.exception))
        # And the file it could read is untouched.
        doc = Document.objects.get(user=self.user, name='q1.md')
        self.assertEqual(doc.content_text, 'revenue up')

    def test_it_writes_inside_its_own_home(self):
        out = vfs.write_file(
            self.scope_raw, f'{AGENT_HOME_ROOT}/Reporter/summary.md', '# Summary')

        self.assertTrue(out['created'])
        self.assertTrue(
            Document.objects.filter(user=self.user, name='summary.md').exists())

    def test_it_cannot_create_a_sibling_of_its_own_home(self):
        """A prefix check that passed on a shorter path would let the agent
        write to `/Agents/` itself, and from there next to every other agent."""
        with self.assertRaises(vfs.VfsError):
            vfs.write_file(self.scope_raw, f'{AGENT_HOME_ROOT}/notes.md', 'x')

        with self.assertRaises(vfs.VfsError):
            vfs.make_dir(self.scope_raw, f'{AGENT_HOME_ROOT}/Impostor')

    def test_it_cannot_write_into_another_agents_home(self):
        with self.assertRaises(vfs.VfsError):
            vfs.write_file(
                self.scope_raw, f'{AGENT_HOME_ROOT}/Rival/plan.md', 'x')

    def test_a_refused_write_creates_no_directories(self):
        """`write_file` does `mkdir -p`. Checking after that would litter the
        user's tree with folders from a write that never happened."""
        with self.assertRaises(vfs.VfsError):
            vfs.write_file(self.scope_raw, 'Finance/2026/Q1/report.md', 'x')

        self.assertFalse(
            Folder.objects.filter(user=self.user, name='2026').exists())

    def test_it_cannot_delete_outside_its_home(self):
        with self.assertRaises(vfs.VfsError):
            vfs.delete(self.scope_raw, 'Finance/q1.md')

        self.assertTrue(
            Document.objects.filter(user=self.user, name='q1.md').exists())

    def test_writability_is_reported_per_directory(self):
        """A flat `scope.writable` would tell the model every folder it lists is
        writable, which it would believe until the write was refused."""
        outside = vfs.list_dir(self.scope_raw, 'Finance')
        inside = vfs.list_dir(self.scope_raw, f'{AGENT_HOME_ROOT}/Reporter')
        root = vfs.list_dir(self.scope_raw, '/')

        self.assertFalse(outside['writable'])
        self.assertTrue(inside['writable'])
        self.assertFalse(root['writable'])

    def test_the_other_modes_still_report_a_single_writability(self):
        """Regression: `may_write_at` must not narrow the modes whose readable
        and writable subtrees are the same."""
        scoped = vfs.build_scope(self.user, vfs.SCOPED, agent_name='S')
        full = vfs.build_scope(self.user, vfs.FULL)
        readonly = vfs.build_scope(self.user, vfs.READONLY)

        self.assertTrue(vfs.list_dir(scoped, '/')['writable'])
        self.assertTrue(vfs.list_dir(full, '/')['writable'])
        self.assertFalse(vfs.list_dir(readonly, '/')['writable'])
        self.assertTrue(full.may_write_at(['Finance', 'deep', 'deeper']))


class FileAccessVocabularyTests(TestCase):
    """The serializer and the filesystem must agree on the same five words.

    They are edited in different apps for different reasons, and the two ways
    they can drift both fail silently. A mode the serializer accepts but
    `build_scope` does not recognise resolves to *no scope*, so the agent saves
    cleanly and then simply has no file tools. A mode `build_scope` serves but
    the serializer rejects is a 400 on save for a setting the UI offers.

    The frontend is the third copy — `FILE_ACCESS_COPY` in
    `better-n8n-frontend/src/types/agentConfig.ts` — which Python cannot import.
    It is kept in step by hand; this test at least guarantees the two halves a
    test *can* reach cannot disagree.
    """

    def test_every_served_mode_is_accepted_by_the_serializer(self):
        from agents.views.agents import FILE_ACCESS

        served = {vfs.READONLY, vfs.SCOPED, vfs.FULL, vfs.READ_ALL_WRITE_OWN}

        self.assertTrue(
            served <= FILE_ACCESS,
            f'{served - FILE_ACCESS} is served by vfs but rejected on save.',
        )

    def test_every_accepted_mode_resolves_to_a_scope_or_is_none(self):
        from agents.views.agents import FILE_ACCESS

        user = User.objects.create_user('vocab', 'v@example.com', 'pw')

        for mode in sorted(FILE_ACCESS):
            with self.subTest(mode=mode):
                scope = vfs.build_scope(user, mode, agent_name='A')
                if mode == 'none':
                    self.assertIsNone(scope)
                else:
                    self.assertIsNotNone(
                        scope,
                        f'"{mode}" is accepted on save but yields no scope, so '
                        f'the agent silently gets no file tools.',
                    )
