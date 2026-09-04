"""
Chat's own file scope, and finding a file once it has been written.

Two things landed together and each is useless without the other. A chat turn
can now write a document — before this, `requires="files"` was an unconditional
`False`, so the tools existed and chat could never see them. And a written
document can be found again by name or content, which is what makes it storage
rather than a drop box: `list_files` alone means walking the tree a directory
at a time, which costs a tool call per guess.

`find` is deliberately not knowledge-base search. It is one indexed substring
match over the user's own rows, so writing a file stays free — the rule this
module's docstring opens with, that folders organise and KBs index, survives
exactly because locating a file never needed an index.
"""
from __future__ import annotations

from django.contrib.auth import get_user_model
from django.test import TestCase

from inference import filesystem as fs, vfs
from inference.models import Document
from workflow_backend.thresholds import CHAT_HOME_ROOT


class ChatScopeTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="chatter", email="c@example.test", password="x"
        )
        self.scope = vfs.chat_scope(self.user)

    def test_the_chat_folder_is_created_and_is_a_root_level_sibling(self):
        """`/Chat/`, not `/Agents/Chat/` — chat is not an agent.

        The tree the user browses is the one place all of this becomes visible,
        so filing chat's output under `Agents` would make that tree lie about
        where the files came from.
        """
        home = fs.child_by_name(self.user, None, CHAT_HOME_ROOT)
        self.assertIsNotNone(home)
        self.assertIsNone(home.parent)

    def test_building_the_scope_twice_reuses_one_folder(self):
        vfs.chat_scope(self.user)
        vfs.chat_scope(self.user)
        roots = [f for f in fs.children(self.user, None) if f.name == CHAT_HOME_ROOT]
        self.assertEqual(len(roots), 1)

    def test_it_reads_the_whole_tree(self):
        reports = fs.create_folder(self.user, "Reports", None)
        Document.objects.create(user=self.user, folder=reports, name="q1.md",
                                file="", file_type="md", file_size=2,
                                content_text="hi", status="stored")
        listing = vfs.list_dir(self.scope, "/Reports")
        self.assertEqual([f["name"] for f in listing["files"]], ["q1.md"])
        # Readable, and plainly not writable — the two halves of the mode.
        self.assertFalse(listing["writable"])

    def test_it_writes_inside_chat(self):
        out = vfs.write_file(self.scope, "/Chat/summary.md", "the summary")
        self.assertTrue(out["created"])
        doc = Document.objects.get(id=out["document_id"])
        self.assertEqual(doc.folder.name, CHAT_HOME_ROOT)
        # Written, never indexed: a chat turn must not start an embedding job.
        self.assertEqual(doc.status, "stored")

    def test_it_refuses_to_write_outside_chat(self):
        """Reading everywhere and writing everywhere are different grants.

        Without this, a conversation could scatter files through a tree the
        user organised themselves, and "where did that come from" would have no
        answer.
        """
        fs.create_folder(self.user, "Reports", None)
        with self.assertRaises(vfs.VfsError):
            vfs.write_file(self.scope, "/Reports/notes.md", "x")

    def test_it_refuses_to_write_at_the_root(self):
        with self.assertRaises(vfs.VfsError):
            vfs.write_file(self.scope, "/loose.md", "x")

    def test_a_later_turn_reads_what_an_earlier_one_wrote(self):
        """No folder per session, on purpose.

        A conversation that cannot read what an earlier one filed is a cabinet
        that forgets, and a user looking for yesterday's summary should not have
        to know a uuid they never saw.
        """
        vfs.write_file(self.scope, "/Chat/notes.md", "remember this")
        later = vfs.chat_scope(self.user)
        self.assertEqual(vfs.read_file(later, "/Chat/notes.md")["content"],
                         "remember this")


class FindTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="finder", email="f@example.test", password="x"
        )
        self.scope = vfs.chat_scope(self.user)
        vfs.write_file(self.scope, "/Chat/quarterly-report.md",
                       "Revenue was up. Margins held.")
        vfs.write_file(self.scope, "/Chat/notes/standup.md",
                       "Discussed the migration plan.")

    def test_a_file_is_found_by_its_name(self):
        out = vfs.find(self.scope, "quarterly")
        self.assertEqual(out["count"], 1)
        self.assertEqual(out["matches"][0]["path"], "/Chat/quarterly-report.md")
        self.assertTrue(out["matches"][0]["in_name"])

    def test_a_file_is_found_by_its_contents(self):
        out = vfs.find(self.scope, "migration")
        self.assertEqual(out["count"], 1)
        self.assertEqual(out["matches"][0]["path"], "/Chat/notes/standup.md")
        # The flag separates the two kinds of hit, so a model can tell "this is
        # the file you named" from "this file mentions it in passing".
        self.assertFalse(out["matches"][0]["in_name"])

    def test_a_miss_says_so_rather_than_returning_an_empty_list(self):
        out = vfs.find(self.scope, "sourdough")
        self.assertEqual(out["count"], 0)
        self.assertIn("No file in scope", out["note"])

    def test_a_one_character_query_is_refused(self):
        # It would match most of the tree and teach the model nothing.
        with self.assertRaises(vfs.VfsError):
            vfs.find(self.scope, "a")

    def test_results_report_where_a_write_is_allowed(self):
        """A file reached by search must describe itself as one reached by a walk.

        `list_dir` reports `writable` per directory; a search result that did
        not would let the model conclude it may edit a file it may only read.
        """
        reports = fs.create_folder(self.user, "Reports", None)
        Document.objects.create(user=self.user, folder=reports, name="external.md",
                                file="", file_type="md", file_size=8,
                                content_text="migration notes", status="stored")

        by_path = {m["path"]: m for m in vfs.find(self.scope, "migration")["matches"]}
        self.assertTrue(by_path["/Chat/notes/standup.md"]["writable"])
        self.assertFalse(by_path["/Reports/external.md"]["writable"])

    def test_another_users_files_are_never_returned(self):
        other = get_user_model().objects.create_user(
            username="stranger", email="s@example.test", password="x"
        )
        other_scope = vfs.chat_scope(other)
        vfs.write_file(other_scope, "/Chat/secret.md", "migration of everything")

        out = vfs.find(self.scope, "migration")
        self.assertEqual([m["path"] for m in out["matches"]],
                         ["/Chat/notes/standup.md"])

    def test_a_trashed_file_is_not_found(self):
        """Trash is a state, not a place — and a file the user cannot see must
        not keep answering searches."""
        from inference import recycle

        doc = Document.objects.get(name="standup.md")
        recycle.trash(self.user, documents=[doc])
        self.assertEqual(vfs.find(self.scope, "migration")["count"], 0)

    def test_a_scoped_search_cannot_see_outside_its_root(self):
        """The confinement holds for search, not only for the walk.

        `/Chat/notes/standup.md` also contains the word and belongs to the same
        user, so a search consulting the whole tree would return it.
        """
        scoped = vfs.build_scope(self.user, vfs.SCOPED, agent_name="Reporter")
        written = vfs.write_file(scoped, "/own.md", "migration inside the home")

        out = vfs.find(scoped, "migration")
        self.assertEqual(out["count"], 1)
        self.assertEqual(out["matches"][0]["document_id"], written["document_id"])
        # Search and write describe the file the same way. Note that under
        # `scoped` this rendered path is a *label*, not a locator: `render`
        # prefixes `scope.label`, while every tool resolves paths relative to
        # the root, so `/Agents/Reporter/own.md` displays but `/own.md` is what
        # reads it back. That predates search — `read_file` has always returned
        # a path in this form — and search matching it is the point here.
        self.assertEqual(out["matches"][0]["path"], written["path"])
        self.assertEqual(vfs.read_file(scoped, "/own.md")["document_id"],
                         written["document_id"])
