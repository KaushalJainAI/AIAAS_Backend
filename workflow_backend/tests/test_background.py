"""
`spawn()` must detach from the request context.

The bug these guard against: a fire-and-forget task started with plain
`create_task` inherits the request's asgiref executor, which is torn down when
the response finishes. The task then dies on its next ORM call with
`RuntimeError: CurrentThreadExecutor already quit or is broken` — long after
the request logged its 200, which is what makes it hard to place.
"""
from __future__ import annotations

import asyncio

from asgiref.sync import AsyncToSync, SyncToAsync
from django.test import SimpleTestCase

from workflow_backend.background import spawn


class SpawnDetachesFromRequestContext(SimpleTestCase):
    """The whole point of `spawn` over `asyncio.create_task`."""

    async def test_does_not_inherit_current_thread_executor(self):
        """
        A `CurrentThreadExecutor` belongs to the sync frame that installed it.
        Inheriting it is what produces the 'already quit or is broken' error
        once that frame — the request — is gone.
        """
        sentinel = object()
        AsyncToSync.executors.current = sentinel
        try:
            seen: list = []

            async def probe() -> None:
                seen.append(getattr(AsyncToSync.executors, "current", None))

            # Baseline: a plain task *does* inherit it. If this ever stops
            # holding, asgiref changed and `spawn` may be redundant.
            await asyncio.ensure_future(probe())
            self.assertIs(seen[0], sentinel)

            await spawn(probe())
            self.assertIsNone(seen[1])
        finally:
            del AsyncToSync.executors.current

    async def test_runs_under_its_own_thread_sensitive_context(self):
        """Its own context means its own executor, living as long as the task."""
        seen: list = []

        async def probe() -> None:
            seen.append(SyncToAsync.thread_sensitive_context.get(None))

        await spawn(probe())
        self.assertIsNotNone(seen[0])

    async def test_thread_sensitive_work_still_runs(self):
        """
        The detached executor must actually be usable. This is the call shape
        every async ORM method uses, so if it works here it works for the ORM.
        """
        from asgiref.sync import sync_to_async

        async def probe() -> str:
            return await sync_to_async(lambda: "ran")()

        self.assertEqual(await spawn(probe()), "ran")

    async def test_exceptions_and_results_propagate_like_create_task(self):
        async def ok() -> str:
            return "value"

        async def boom() -> None:
            raise ValueError("expected")

        self.assertEqual(await spawn(ok()), "value")
        with self.assertRaises(ValueError):
            await spawn(boom())

    async def test_cancellation_propagates_into_the_coroutine(self):
        """`runs.stop()` depends on this: cancelling the task cancels the turn."""
        entered = asyncio.Event()
        cancelled = False

        async def sleeper() -> None:
            nonlocal cancelled
            entered.set()
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                cancelled = True
                raise

        task = spawn(sleeper())
        await entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(cancelled)
