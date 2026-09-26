"""
The VFS hardening pass (docs/VFS_HARDENING_PLAN.md), one class per item.
"""
from __future__ import annotations

from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase

from inference import filesystem as fs
from inference import vfs
from inference.models import Document, Folder
from workflow_backend.thresholds import AGENT_HOME_ROOT, CHAT_HOME_ROOT

User = get_user_model()


class Base(TestCase):
    def setUp(self):
        self.user = User.objects.create_user('owner', 'owner@example.com', 'pw')
        self.full = vfs.build_scope(self.user, vfs.FULL)

    def own(self, name='Bot'):
        return vfs.build_scope(self.user, vfs.READ_ALL_WRITE_OWN, agent_name=name)

    def doc(self, path):
        parts = vfs.segments(path)
        folder = None
        for seg in parts[:-1]:
            folder = fs.child_by_name(self.user, folder, seg)
        return Document.objects.filter(user=self.user, folder=folder, name=parts[-1])


# 1 ─ duplicate names ────────────────────────────────────────────────────────

class DuplicateNameTests(Base):
    def test_existing_duplicates_resolve_to_the_oldest_every_time(self):
        a = Document.objects.create(user=self.user, name='x.md', content_text='first',
                                    file_size=5, status='stored')
        Document.objects.create(user=self.user, name='x.md', content_text='second',
                                file_size=6, status='stored')
        self.assertEqual(vfs.read_file(self.full, '/x.md')['content'], 'first')
        vfs.write_file(self.full, '/x.md', 'new')
        a.refresh_from_db()
        self.assertEqual(a.content_text, 'new')

    def test_every_write_path_takes_the_name_lock(self):
        calls = []
        real = vfs._name_lock

        def spy(user, folder, name):
            calls.append(name)
            return real(user, folder, name)

        with mock.patch.object(vfs, '_name_lock', side_effect=spy):
            vfs.write_file(self.full, '/a.md', 'x')
            vfs.edit_file(self.full, '/a.md', 'x', 'y')
            vfs.write_binary(self.full, '/p.png', b'\x89PNG\r\n\x1a\nxx')
            vfs.move(self.full, '/a.md', '/b.md')
            vfs.copy(self.full, '/b.md', '/c.md')
        self.assertEqual(calls, ['a.md', 'a.md', 'p.png', 'b.md', 'c.md'])

    def test_a_second_write_of_a_new_path_updates_rather_than_duplicates(self):
        vfs.write_file(self.full, '/n.md', 'one')
        vfs.write_file(self.full, '/n.md', 'two')
        self.assertEqual(self.doc('/n.md').count(), 1)


# 2 ─ lost updates ───────────────────────────────────────────────────────────

class ExpectedVersionTests(Base):
    def setUp(self):
        super().setUp()
        vfs.write_file(self.full, '/f.md', 'alpha beta')
        self.v = vfs.read_file(self.full, '/f.md')['version']

    def test_fresh_version_writes(self):
        out = vfs.write_file(self.full, '/f.md', 'new', expected_version=self.v)
        self.assertNotEqual(out['version'], self.v)

    def test_stale_version_is_refused_and_nothing_changes(self):
        vfs.edit_file(self.full, '/f.md', 'alpha', 'ALPHA')      # someone else
        with self.assertRaisesRegex(vfs.VfsError, 'changed since you read it'):
            vfs.write_file(self.full, '/f.md', 'mine', expected_version=self.v)
        with self.assertRaisesRegex(vfs.VfsError, 'changed since you read it'):
            vfs.edit_file(self.full, '/f.md', 'beta', 'B', expected_version=self.v)
        self.assertEqual(self.doc('/f.md').get().content_text, 'ALPHA beta')

    def test_version_of_a_deleted_file_is_refused(self):
        vfs.delete(self.full, '/f.md')
        with self.assertRaisesRegex(vfs.VfsError, 'no longer exists'):
            vfs.write_file(self.full, '/f.md', 'x', expected_version=self.v)
        with self.assertRaisesRegex(vfs.VfsError, 'no longer exists'):
            vfs.edit_file(self.full, '/f.md', 'a', 'b', expected_version=self.v)

    def test_absent_version_keeps_the_old_behaviour(self):
        vfs.edit_file(self.full, '/f.md', 'alpha', 'ALPHA')
        vfs.write_file(self.full, '/f.md', 'overwritten')
        self.assertEqual(self.doc('/f.md').get().content_text, 'overwritten')

    def test_chained_versions_from_write_results_work(self):
        v = vfs.edit_file(self.full, '/f.md', 'alpha', 'A', expected_version=self.v)['version']
        vfs.edit_file(self.full, '/f.md', 'beta', 'B', expected_version=v)
        self.assertEqual(self.doc('/f.md').get().content_text, 'A B')


# 3 ─ find snippets ──────────────────────────────────────────────────────────

class FindSnippetTests(Base):
    def test_snippets_carry_line_numbers(self):
        vfs.write_file(self.full, '/r.md', 'intro\nthe Revenue line\nx\nrevenue again\n')
        match = vfs.find(self.full, 'revenue')['matches'][0]
        self.assertEqual([s['line'] for s in match['snippets']], [2, 4])
        self.assertEqual(match['snippets'][0]['text'], 'the Revenue line')

    def test_snippets_are_capped_and_long_lines_centred(self):
        vfs.write_file(self.full, '/l.md', ('a' * 500 + 'NEEDLE' + 'b' * 500 + '\n') * 5)
        snippets = vfs.find(self.full, 'needle')['matches'][0]['snippets']
        self.assertEqual(len(snippets), 3)
        self.assertIn('NEEDLE', snippets[0]['text'])
        self.assertLess(len(snippets[0]['text']), 210)

    def test_name_only_match_has_no_snippets(self):
        vfs.write_file(self.full, '/budget.md', 'nothing here')
        self.assertEqual(vfs.find(self.full, 'budget')['matches'][0]['snippets'], [])


# 4 ─ move / copy ────────────────────────────────────────────────────────────

class MoveTests(Base):
    def test_rename_keeps_id_and_history(self):
        out = vfs.write_file(self.full, '/a.md', 'v1')
        vfs.write_file(self.full, '/a.md', 'v2')
        before = vfs.file_versions(self.full, '/a.md')['versions']
        res = vfs.move(self.full, '/a.md', '/notes/b.md')
        self.assertEqual(res['document_id'], out['document_id'])
        self.assertEqual(res['to'], '/notes/b.md')
        self.assertEqual(vfs.read_file(self.full, '/notes/b.md')['content'], 'v2')
        self.assertEqual(vfs.file_versions(self.full, '/notes/b.md')['versions'], before)
        self.assertFalse(self.doc('/a.md').exists())

    def test_into_an_existing_directory_keeps_the_name(self):
        vfs.write_file(self.full, '/a.md', 'x')
        vfs.make_dir(self.full, '/dir')
        self.assertEqual(vfs.move(self.full, '/a.md', '/dir')['to'], '/dir/a.md')

    def test_taken_destination_is_refused(self):
        vfs.write_file(self.full, '/a.md', 'x')
        vfs.write_file(self.full, '/b.md', 'y')
        with self.assertRaisesRegex(vfs.VfsError, 'already exists'):
            vfs.move(self.full, '/a.md', '/b.md')

    def test_directory_move_and_rename(self):
        vfs.write_file(self.full, '/p/q/f.md', 'x')
        vfs.move(self.full, '/p/q', '/r/q2')
        self.assertEqual(vfs.read_file(self.full, '/r/q2/f.md')['content'], 'x')
        vfs.move(self.full, '/r/q2', '/r/q3')
        self.assertEqual(vfs.read_file(self.full, '/r/q3/f.md')['content'], 'x')

    def test_folder_into_itself_is_refused_without_creating_folders(self):
        vfs.write_file(self.full, '/p/f.md', 'x')
        count = Folder.objects.filter(user=self.user).count()
        with self.assertRaisesRegex(vfs.VfsError, 'into itself'):
            vfs.move(self.full, '/p', '/p/sub/p')
        self.assertEqual(Folder.objects.filter(user=self.user).count(), count)

    def test_binary_extension_cannot_change(self):
        vfs.write_binary(self.full, '/p.png', b'\x89PNG\r\n\x1a\nxx')
        with self.assertRaisesRegex(vfs.VfsError, 'Keep the .png'):
            vfs.move(self.full, '/p.png', '/p.txt')

    def test_text_extension_change_retypes(self):
        vfs.write_file(self.full, '/a.txt', 'x')
        vfs.move(self.full, '/a.txt', '/a.md')
        self.assertEqual(self.doc('/a.md').get().file_type, 'md')

    def test_system_folders_cannot_be_moved(self):
        vfs.chat_scope(self.user)
        with self.assertRaisesRegex(vfs.VfsError, 'system folder'):
            vfs.move(self.full, f'/{CHAT_HOME_ROOT}', '/elsewhere')

    def test_read_all_write_own_needs_both_ends_writable(self):
        scope = self.own()
        vfs.write_file(self.full, '/mine.md', 'x')
        home = f'/{AGENT_HOME_ROOT}/Bot'
        with self.assertRaisesRegex(vfs.VfsError, 'can only'):
            vfs.move(scope, '/mine.md', f'{home}/mine.md')
        vfs.write_file(scope, f'{home}/a.md', 'x')
        with self.assertRaisesRegex(vfs.VfsError, 'can only'):
            vfs.move(scope, f'{home}/a.md', '/a.md')
        with self.assertRaisesRegex(vfs.VfsError, 'can only'):
            vfs.move(scope, home, '/Bot2')   # its own home: parent unwritable
        vfs.move(scope, f'{home}/a.md', f'{home}/sub/b.md')

    def test_readonly_scope_cannot_move(self):
        vfs.write_file(self.full, '/a.md', 'x')
        ro = vfs.build_scope(self.user, vfs.READONLY)
        with self.assertRaisesRegex(vfs.VfsError, 'read-only'):
            vfs.move(ro, '/a.md', '/b.md')


class CopyTests(Base):
    def test_copy_text_and_binary(self):
        vfs.write_file(self.full, '/a.md', 'body')
        out = vfs.copy(self.full, '/a.md', '/b/a copy.md')
        self.assertEqual(vfs.read_file(self.full, '/b/a copy.md')['content'], 'body')
        self.assertNotEqual(out['document_id'], self.doc('/a.md').get().id)
        vfs.write_binary(self.full, '/p.png', b'\x89PNG\r\n\x1a\nxx')
        vfs.copy(self.full, '/p.png', '/b')
        self.assertEqual(vfs.read_binary(self.full, '/b/p.png'), b'\x89PNG\r\n\x1a\nxx')

    def test_copy_refuses_taken_names_and_directories(self):
        vfs.write_file(self.full, '/a.md', 'x')
        with self.assertRaisesRegex(vfs.VfsError, 'already exists'):
            vfs.copy(self.full, '/a.md', '/a.md')
        vfs.make_dir(self.full, '/d')
        with self.assertRaisesRegex(vfs.VfsError, 'not directories'):
            vfs.copy(self.full, '/d', '/e')

    def test_read_all_write_own_copies_in_but_not_out(self):
        scope = self.own()
        vfs.write_file(self.full, '/src.md', 'x')
        home = f'/{AGENT_HOME_ROOT}/Bot'
        vfs.copy(scope, '/src.md', f'{home}/src.md')
        with self.assertRaisesRegex(vfs.VfsError, 'can only'):
            vfs.copy(scope, f'{home}/src.md', '/copy.md')


# 5 ─ line reads ─────────────────────────────────────────────────────────────

class LineReadTests(Base):
    def setUp(self):
        super().setUp()
        vfs.write_file(self.full, '/l.md', '\n'.join(f'row {i}' for i in range(1, 11)))

    def test_numbered_range(self):
        out = vfs.read_file(self.full, '/l.md', start_line=3, end_line=4)
        self.assertEqual(out['content'], '3: row 3\n4: row 4')
        self.assertEqual(out['total_lines'], 10)
        self.assertNotIn('truncated', out)

    def test_window_cap_names_the_next_line(self):
        out = vfs.read_file(self.full, '/l.md', start_line=1, window=20)
        self.assertTrue(out['truncated'])
        self.assertIn(f'start_line={out["end_line"] + 1}', out['note'])

    def test_past_the_end_is_an_error(self):
        with self.assertRaisesRegex(vfs.VfsError, '10 lines'):
            vfs.read_file(self.full, '/l.md', start_line=11)

    def test_char_mode_is_unchanged(self):
        self.assertEqual(vfs.read_file(self.full, '/l.md', offset=0)['content'][:5], 'row 1')


# 6 ─ case ───────────────────────────────────────────────────────────────────

class CaseTests(Base):
    def test_read_hints_the_other_case(self):
        vfs.write_file(self.full, '/Reports/Q1.md', 'x')
        with self.assertRaisesRegex(vfs.VfsError, r'Did you mean /Reports\?'):
            vfs.read_file(self.full, '/reports/Q1.md')
        with self.assertRaisesRegex(vfs.VfsError, r'Did you mean /Reports/Q1.md\?'):
            vfs.read_file(self.full, '/Reports/q1.md')

    def test_write_reuses_a_single_other_case_folder(self):
        vfs.write_file(self.full, '/Reports/a.md', 'x')
        out = vfs.write_file(self.full, '/reports/b.md', 'y')
        self.assertEqual(out['path'], '/Reports/b.md')
        self.assertEqual(Folder.objects.filter(user=self.user, name__iexact='reports').count(), 1)

    def test_ambiguous_case_creates_the_exact_name(self):
        fs.create_folder(self.user, 'Reports', None)
        fs.create_folder(self.user, 'REPORTS', None)
        vfs.write_file(self.full, '/reports/a.md', 'x')
        self.assertTrue(Folder.objects.filter(user=self.user, name='reports').exists())


# 7 ─ depth ──────────────────────────────────────────────────────────────────

class DepthListingTests(Base):
    def setUp(self):
        super().setUp()
        vfs.write_file(self.full, '/a/b/c/deep.md', 'x')
        vfs.write_file(self.full, '/a/top.md', 'x')

    def test_depth_one_is_the_old_answer(self):
        self.assertNotIn('tree', vfs.list_dir(self.full, '/a'))

    def test_tree_goes_the_requested_depth(self):
        tree = vfs.list_dir(self.full, '/a', depth=2)['tree']
        self.assertEqual(tree, ['b/', 'b/c/', 'top.md'])
        tree3 = vfs.list_dir(self.full, '/a', depth=3)['tree']
        self.assertIn('b/c/deep.md', tree3)

    def test_tree_budget(self):
        out = vfs.list_dir(self.full, '/a', limit=2, depth=3)
        self.assertEqual(len(out['tree']), 2)
        self.assertTrue(out['tree_truncated'])
