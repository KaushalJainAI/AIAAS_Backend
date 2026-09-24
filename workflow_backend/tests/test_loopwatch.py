"""
Phase 0 tests (`docs/CONCURRENCY_LAG_FIX_PLAN.md`): the loop-lag watchdog.

Runs everywhere, including SQLite dev: the watcher must stay silent where
there is no pool, and starting it twice must still start it once.
"""
from __future__ import annotations

import asyncio

from django.test import SimpleTestCase


class LoopwatchTests(SimpleTestCase):
    async def test_pool_stats_none_without_pool(self):
        """No pool configured means no data, not an error in the measurer."""
        from workflow_backend import loopwatch

        self.assertIsNone(loopwatch._pool_stats())

    async def test_ensure_started_spawns_once(self):
        from workflow_backend import loopwatch

        loopwatch._started = False
        try:
            loopwatch.ensure_started()
            loopwatch.ensure_started()
            watchers = [
                t for t in asyncio.all_tasks() if t.get_name() == "loopwatch"
            ]
            self.assertEqual(len(watchers), 1)
            # Let the task take its first step (into its 1 s sleep) so that
            # cancelling it below does not strand a never-awaited coroutine.
            await asyncio.sleep(0)
        finally:
            watchers = [
                t for t in asyncio.all_tasks() if t.get_name() == "loopwatch"
            ]
            for task in watchers:
                task.cancel()
            if watchers:
                await asyncio.gather(*watchers, return_exceptions=True)
            loopwatch._started = False
