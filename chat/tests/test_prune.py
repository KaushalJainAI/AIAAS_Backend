"""
Phase 4 (`docs/CONCURRENCY_LAG_FIX_PLAN.md`): prune old chat checkpoints.

The sweep itself only runs on the Postgres saver, which no local run has —
so what is pinned here is the policy: what counts as a chat thread, which
rows a pass deletes, and that anything but Postgres reports `skipped` rather
than touching a schema it does not speak.
"""
from __future__ import annotations

from asgiref.sync import async_to_sync
from django.core.management import call_command
from django.test import SimpleTestCase

from chat.turn.prune import is_chat_thread, plan_prune, prune_chat_checkpoints


def _entry(cid: str, step, versions=()):
    return {'checkpoint_id': cid, 'step': step, 'versions': list(versions)}


class ChatThreadTests(SimpleTestCase):
    def test_agent_threads_are_not_chat(self):
        self.assertFalse(is_chat_thread('agent-12-abc123'))
        self.assertFalse(is_chat_thread('agent-'))

    def test_session_and_nomem_threads_are_chat(self):
        self.assertTrue(is_chat_thread('550e8400-e29b-41d4-a716-446655440000'))
        self.assertTrue(is_chat_thread('550e8400:nomem:abc123'))

    def test_worker_threads_fall_on_the_chat_side(self):
        # `sub-` workers are pruned too: resume reads the latest, which is
        # always kept, so there is nothing to protect.
        self.assertTrue(is_chat_thread('sub-agent-12-0-abc123'))


class PlanPruneTests(SimpleTestCase):
    def test_few_checkpoints_prunes_nothing(self):
        prune, kept = plan_prune([_entry('a', 0, [1]), _entry('b', 1, [2])], keep=3)
        self.assertEqual((prune, kept), ([], {1, 2}))

    def test_oldest_by_step_goes_first(self):
        entries = [_entry(f'c{i}', i, [i]) for i in range(5)]
        prune, kept = plan_prune(entries, keep=3)
        self.assertEqual(prune, ['c1', 'c0'])
        self.assertEqual(kept, {2, 3, 4})

    def test_step_order_beats_id_order(self):
        entries = [_entry('new-id', 0, [9]), _entry('old-id', 4, [1])]
        prune, kept = plan_prune(entries, keep=1)
        self.assertEqual((prune, kept), (['new-id'], {1}))

    def test_an_unorderable_row_is_never_pruned(self):
        # No step might mean the latest; deleting it is the unforgivable outcome.
        entries = [_entry('mystery', None, [7])] + [
            _entry(f'c{i}', i, [i]) for i in range(4)]
        prune, kept = plan_prune(entries, keep=3)
        self.assertNotIn('mystery', prune)
        self.assertIn(7, kept)
        self.assertEqual(sorted(prune), ['c0'])

    def test_kept_versions_cover_all_survivors(self):
        entries = [_entry('a', 0, [1, 2]), _entry('b', 1, [2, 3]),
                   _entry('c', 2, [3, 4])]
        prune, kept = plan_prune(entries, keep=2)
        self.assertEqual(prune, ['a'])
        self.assertEqual(kept, {2, 3, 4})


class SkipOffPostgresTests(SimpleTestCase):
    def test_sweep_skips_on_memory(self):
        tally = async_to_sync(prune_chat_checkpoints)(dry_run=True)
        self.assertEqual(tally['status'], 'skipped')

    def test_command_reports_the_skip(self):
        out = []

        class Writer:
            def write(self, msg):
                out.append(msg)

        call_command('prune_chat_checkpoints', stdout=Writer())
        self.assertTrue(any('Skipped' in line for line in out))
