"""
The shared workspace: a second writable subtree, and everywhere it refuses.

Delegation used to be able to hand back only prose. A worker's findings came
home through the transcript, so every fan-out paid its whole result into the
parent's context window — and anything over the cap was archived and had to be
fetched back out again. A folder both can write to makes the answer "wrote
findings-2.md", and the parent reads it only if it needs to.

The interesting half is the refusals. A second writable subtree is the first
thing in this module that widens what a scope may write, so each case where it
declines is load-bearing: a worker's configuration must mean the same thing
whoever called it, and being delegated to is not consent to change it.
"""
from __future__ import annotations

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase

from inference import vfs
from inference.vfs import FileScope, with_shared_workspace


def scope(**kwargs) -> FileScope:
    base = dict(user=None, root=None, mode=vfs.READ_ALL_WRITE_OWN, label='/',
                write_prefix=('Agents', 'Worker'), write_label='/Agents/Worker')
    return FileScope(**{**base, **kwargs})


class GrantTests(SimpleTestCase):
    def test_the_parents_folder_becomes_writable(self):
        out = with_shared_workspace(scope(), ('Agents', 'Reporter'))

        self.assertEqual(out.shared_prefix, ('Agents', 'Reporter'))
        self.assertEqual(out.shared_label, '/Agents/Reporter')
        self.assertTrue(out.may_write_at(['Agents', 'Reporter', 'findings.md']))

    def test_the_workers_own_folder_still_works(self):
        """It adds a subtree; it never moves the one that was there."""
        out = with_shared_workspace(scope(), ('Agents', 'Reporter'))
        self.assertTrue(out.may_write_at(['Agents', 'Worker', 'notes.md']))

    def test_nothing_else_becomes_writable(self):
        out = with_shared_workspace(scope(), ('Agents', 'Reporter'))

        for path in (['Notes', 'x.md'], ['Agents', 'Someone', 'x.md'],
                     ['Agents'], ['Chat', 'x.md'], []):
            with self.subTest(path=path):
                self.assertFalse(out.may_write_at(path))

    def test_a_label_can_be_given_or_derived(self):
        derived = with_shared_workspace(scope(), ('Chat',))
        self.assertEqual(derived.shared_label, '/Chat')

        given = with_shared_workspace(scope(), ('Chat',), '/Chat (shared)')
        self.assertEqual(given.shared_label, '/Chat (shared)')

    def test_chat_can_be_the_delegator(self):
        """The prefix comes from the caller's scope, not from an agent name.

        Chat's folder is `/Chat/` and not an agent home at all, so a rule built
        around agent names would have silently done nothing here.
        """
        out = with_shared_workspace(scope(), ('Chat',))
        self.assertTrue(out.may_write_at(['Chat', 'report.md']))


class RefusalTests(SimpleTestCase):
    """Four cases where the workspace is not added, each for its own reason."""

    def test_a_scoped_worker_is_untouched(self):
        """Its configuration says "confine me to my folder".

        Re-rooting it would make one brief write to two different places
        depending on who called it, which is a bug report waiting to happen.
        """
        confined = scope(root=object(), mode=vfs.SCOPED,
                         label='/Agents/Worker', write_prefix=())
        self.assertIs(with_shared_workspace(confined, ('Agents', 'Reporter')),
                      confined)

    def test_a_readonly_worker_is_untouched(self):
        """Being delegated to is not consent to start writing."""
        reader = scope(mode=vfs.READONLY, write_prefix=None, write_label='')
        out = with_shared_workspace(reader, ('Agents', 'Reporter'))

        self.assertIs(out, reader)
        self.assertFalse(out.writable)

    def test_an_empty_prefix_is_refused(self):
        """`()` matches every path.

        A parent that writes everywhere has no particular folder to share, and
        passing its prefix through would quietly upgrade the worker to write
        anywhere in the tree — the one outcome this must never produce.
        """
        out = with_shared_workspace(scope(), ())
        self.assertIsNone(out.shared_prefix)
        self.assertFalse(out.may_write_at(['anything', 'at', 'all']))

    def test_sharing_the_workers_own_folder_changes_nothing(self):
        out = with_shared_workspace(scope(), ('Agents', 'Worker'))
        self.assertIsNone(out.shared_prefix)

    def test_no_scope_stays_no_scope(self):
        self.assertIsNone(with_shared_workspace(None, ('Agents', 'R')))


class WritableFlagTests(SimpleTestCase):
    def test_a_scope_with_only_a_shared_subtree_is_writable(self):
        """`writable` gates whether the write tools are offered at all.

        Reading it from `write_prefix` alone would withhold them from a worker
        whose only writable place is the shared folder.
        """
        odd = FileScope(user=None, root=None, mode=vfs.READ_ALL_WRITE_OWN,
                        label='/', write_prefix=None,
                        shared_prefix=('Agents', 'Reporter'),
                        shared_label='/Agents/Reporter')
        self.assertTrue(odd.writable)


class EndToEndTests(TestCase):
    """Through the real tree, because segment maths is not the whole story."""

    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="ws", email="ws@example.test", password="x"
        )

    def test_a_worker_can_write_into_the_delegating_agents_home(self):
        from inference.models import Document

        # Both homes exist, as they would after each agent's first run.
        vfs.agent_home(self.user, "Reporter")
        worker = vfs.build_scope(self.user, vfs.READ_ALL_WRITE_OWN,
                                 agent_name="Worker")
        shared = with_shared_workspace(worker, ("Agents", "Reporter"))

        result = vfs.write_file(shared, "/Agents/Reporter/findings.md",
                                "the numbers")

        self.assertTrue(result["created"])
        doc = Document.objects.get(user=self.user, name="findings.md")
        self.assertEqual(doc.folder.name, "Reporter")
        self.assertEqual(doc.content_text, "the numbers")

    def test_without_the_workspace_that_same_write_is_refused(self):
        """Guards the test above from passing because everything is writable."""
        vfs.agent_home(self.user, "Reporter")
        worker = vfs.build_scope(self.user, vfs.READ_ALL_WRITE_OWN,
                                 agent_name="Worker")

        with self.assertRaises(vfs.VfsError):
            vfs.write_file(worker, "/Agents/Reporter/findings.md", "nope")

    def test_the_worker_still_cannot_write_outside_both_folders(self):
        vfs.agent_home(self.user, "Reporter")
        worker = vfs.build_scope(self.user, vfs.READ_ALL_WRITE_OWN,
                                 agent_name="Worker")
        shared = with_shared_workspace(worker, ("Agents", "Reporter"))

        with self.assertRaises(vfs.VfsError):
            vfs.write_file(shared, "/Notes/secret.md", "nope")

    def test_a_listing_reports_the_shared_folder_as_writable(self):
        """`list_dir` reports `writable` per directory, so a model planning
        where to put something must see the shared folder as a candidate."""
        vfs.agent_home(self.user, "Reporter")
        worker = vfs.build_scope(self.user, vfs.READ_ALL_WRITE_OWN,
                                 agent_name="Worker")
        shared = with_shared_workspace(worker, ("Agents", "Reporter"))

        listing = vfs.list_dir(shared, "/Agents/Reporter")
        self.assertTrue(listing["writable"])
