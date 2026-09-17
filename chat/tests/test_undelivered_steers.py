"""
A steer the run never read is handed back, never kept for a later turn.

Steers are delivered on the `tools -> steering -> agent` edge. A message that
arrives while the model is writing its final answer — no more tool calls, so
no more boundaries — used to stay in the mailbox after the turn ended. The API
had already told the user it was accepted; the client reset its queued counter
to zero; and because chat never cleared the mailbox, the message was delivered
in the middle of the user's *next* turn on the session, possibly about
something else entirely.

The fix is at the one place every chat turn ends, `runs.finish`: anything left
is drained and sent back to the client as a `steers_returned` frame, so the
client can put the text back in front of the user.
"""
from __future__ import annotations

import asyncio

from asgiref.sync import async_to_sync
from django.test import SimpleTestCase

from chat.turn import runs, steering
from chat.turn.events import Event


def _frames_of(run: runs.ChatRun, event: Event) -> list[dict]:
    return [payload for kind, payload in run.frames if kind == event]


class UndeliveredSteersTests(SimpleTestCase):
    def setUp(self):
        steering.clear()
        runs.clear()
        self.addCleanup(steering.clear)
        self.addCleanup(runs.clear)

    def _turn_that_ignores_a_steer(self, *, fail: bool = False, key: str = "session-1") -> runs.ChatRun:
        """Run a turn with no tool boundary; steer it while it is answering."""
        async def go():
            answering = asyncio.Event()
            release = asyncio.Event()

            async def work(sink):
                answering.set()
                await release.wait()  # the model writing its final answer
                if fail:
                    raise RuntimeError("provider went away")
                await sink(Event.DONE, {})

            run = runs.start(key, user_id=1, work=work)
            await answering.wait()
            self.assertTrue(steering.post(key, "also check pricing"))
            self.assertTrue(steering.post(key, "and the changelog"))
            release.set()
            await run.task
            return run

        return async_to_sync(go)()

    def test_a_steer_the_run_never_read_is_returned_to_the_client(self):
        run = self._turn_that_ignores_a_steer()

        [returned] = _frames_of(run, Event.STEERS_RETURNED)
        self.assertEqual(returned["messages"], ["also check pricing", "and the changelog"])

    def test_it_is_returned_after_done_so_the_client_has_settled_the_turn(self):
        run = self._turn_that_ignores_a_steer()

        kinds = [kind for kind, _ in run.frames]
        self.assertLess(kinds.index(Event.DONE), kinds.index(Event.STEERS_RETURNED))

    def test_it_is_not_kept_for_the_next_turn(self):
        self._turn_that_ignores_a_steer()

        self.assertFalse(steering.pending("session-1"))
        self.assertEqual(steering.take("session-1"), "")

    def test_a_failed_turn_returns_it_too(self):
        run = self._turn_that_ignores_a_steer(fail=True)

        self.assertEqual(run.status, "error")
        [returned] = _frames_of(run, Event.STEERS_RETURNED)
        self.assertEqual(len(returned["messages"]), 2)

    def test_a_turn_with_nothing_left_emits_no_frame(self):
        async def go():
            async def work(sink):
                await sink(Event.DONE, {})

            run = runs.start("quiet", user_id=1, work=work)
            await run.task
            return run

        run = async_to_sync(go)()
        self.assertEqual(_frames_of(run, Event.STEERS_RETURNED), [])

    def test_the_autonomy_level_is_not_drained_with_the_messages(self):
        # A mode is a standing answer for the session, not an instruction to
        # act on once; returning the steers must not reset it.
        steering.set_autonomy("session-1", "auto")
        self._turn_that_ignores_a_steer()

        self.assertEqual(steering.autonomy("session-1"), "auto")

    def test_a_stale_steer_left_by_an_earlier_process_is_dropped_at_turn_start(self):
        # Belt and braces: anything still queued when a *new* turn starts was
        # meant for a turn that is over, so it must not land mid-way through
        # this one.
        steering.post("session-2", "meant for a turn that is gone")

        async def go():
            async def work(sink):
                await sink(Event.DONE, {})

            run = runs.start("session-2", user_id=1, work=work)
            await run.task
            return run

        run = async_to_sync(go)()
        self.assertFalse(steering.pending("session-2"))
        self.assertEqual(_frames_of(run, Event.STEERS_RETURNED), [])
