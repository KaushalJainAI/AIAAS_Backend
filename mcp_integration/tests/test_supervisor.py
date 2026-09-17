"""Connector subprocesses are admitted against a memory budget, not a count.

The pool cap (`test_pool_lru.py`) is a cache policy: it decides which *already
connected* session to drop. It could not prevent the 2026-09-16 OOM because
nothing it counts existed yet — eight connectors were all mid-start, each
spawning two Node processes, inside a 384 MB container.

What is pinned here is the half that was missing:

* admission happens **before** a spawn, and reserves for starts in flight;
* a budget is in **megabytes**, so six cheap connectors and six expensive ones
  are not the same claim;
* over budget, the supervisor **evicts an idle session and retries**, and only
  refuses when there is nothing left to give;
* the container's own limit is a **backstop** — connectors lose before daphne
  does; and
* eviction **kills the process tree**, because a budget that accounts for
  memory a cancelled transport never actually released is decoration.
"""
from __future__ import annotations

import asyncio
import os
from unittest.mock import patch

from asgiref.sync import async_to_sync
from django.test import SimpleTestCase

from mcp_integration import supervisor as sup
from mcp_integration.supervisor import ConnectorBudgetExceeded, ConnectorSupervisor


class BudgetAdmissionTests(SimpleTestCase):
    def setUp(self) -> None:
        self.sup = ConnectorSupervisor()
        # No /proc reading in unit tests: usage is whatever the fallback
        # accounting says, which is what a non-Linux dev box gets too.
        patcher = patch.object(sup, "proc_available", return_value=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        pressure = patch.object(sup, "container_headroom_mb", return_value=None)
        pressure.start()
        self.addCleanup(pressure.stop)

    async def _admit(self, key, server_id, evict=None):
        async with self.sup.admit(key, server_id, evict_lru=evict) as handle:
            handle.commit([])
        return True

    def test_a_start_is_refused_when_the_budget_is_full(self):
        with patch.object(sup, "MEMORY_BUDGET_MB", 100.0), \
             patch.object(sup, "DEFAULT_CONNECTOR_MB", 70.0), \
             patch.object(sup, "ADMIT_WAIT_SECONDS", 0.05):
            async_to_sync(self._admit)((1, 1), 1)          # 70 of 100
            with self.assertRaises(ConnectorBudgetExceeded) as ctx:
                async_to_sync(self._admit)((2, 1), 2)      # would be 140
        self.assertIn("budget", str(ctx.exception).lower())

    def test_over_budget_it_evicts_and_then_admits(self):
        """The refusal is a last resort: an idle session is memory we can reclaim."""
        evicted: list[bool] = []

        def _evict() -> bool:
            # Mirror what the pool does: drop the session, which releases it.
            if evicted:
                return False
            evicted.append(True)
            self.sup.release((1, 1), kill=False)
            return True

        with patch.object(sup, "MEMORY_BUDGET_MB", 100.0), \
             patch.object(sup, "DEFAULT_CONNECTOR_MB", 70.0), \
             patch.object(sup, "ADMIT_WAIT_SECONDS", 0.5):
            async_to_sync(self._admit)((1, 1), 1)
            async_to_sync(self._admit)((2, 1), 2, _evict)

        self.assertTrue(evicted, "should have tried eviction before refusing")

    def test_starts_in_flight_count_against_the_budget(self):
        """The failure this whole module exists for: N concurrent cold starts.

        Each reserves before it spawns, so the second one sees the first even
        though no session has connected yet — which is exactly the accounting
        the pool cap could not do.
        """
        started = asyncio.Event()

        async def _scenario():
            async def _hold():
                async with self.sup.admit((1, 1), 1):
                    started.set()
                    await asyncio.sleep(0.2)

            task = asyncio.ensure_future(_hold())
            await started.wait()
            with self.assertRaises(ConnectorBudgetExceeded):
                async with self.sup.admit((2, 1), 2):
                    pass  # pragma: no cover — admission must refuse first
            await task

        with patch.object(sup, "MEMORY_BUDGET_MB", 100.0), \
             patch.object(sup, "DEFAULT_CONNECTOR_MB", 70.0), \
             patch.object(sup, "MAX_CONCURRENT_STARTS", 2), \
             patch.object(sup, "ADMIT_WAIT_SECONDS", 0.05):
            async_to_sync(_scenario)()

    def test_only_one_connector_starts_at_a_time(self):
        """Starting is the expensive moment, so it is serialised."""
        concurrent = 0
        peak = 0

        async def _start(key):
            nonlocal concurrent, peak
            async with self.sup.admit(key, key[0]):
                concurrent += 1
                peak = max(peak, concurrent)
                await asyncio.sleep(0.02)
                concurrent -= 1

        async def _scenario():
            await asyncio.gather(*(_start((i, 1)) for i in range(4)))

        with patch.object(sup, "MEMORY_BUDGET_MB", 0.0), \
             patch.object(sup, "MAX_CONCURRENT_STARTS", 1), \
             patch.object(sup, "ADMIT_WAIT_SECONDS", 5.0):
            async_to_sync(_scenario)()

        self.assertEqual(peak, 1)

    def test_container_headroom_refuses_even_under_budget(self):
        """Something has to lose, and it is not the web server."""
        with patch.object(sup, "MEMORY_BUDGET_MB", 1000.0), \
             patch.object(sup, "DEFAULT_CONNECTOR_MB", 110.0), \
             patch.object(sup, "ADMIT_WAIT_SECONDS", 0.05), \
             patch.object(sup, "container_headroom_mb", return_value=20.0):
            with self.assertRaises(ConnectorBudgetExceeded) as ctx:
                async_to_sync(self._admit)((1, 1), 1)
        self.assertIn("container has", str(ctx.exception))

    def test_headroom_counts_what_is_about_to_be_spent(self):
        """A *fraction* answers the question one start too late.

        daphne at 220 MB plus a 70 MB connector is 76% of a 384 MB container —
        under any sane ceiling — and the next connector is the Gmail one at
        ~150 MB. Admitting it takes the container to 440 MB and the kernel
        kills daphne, with nothing ever having looked over the high-water mark.
        """
        with patch.object(sup, "MEMORY_BUDGET_MB", 0.0), \
             patch.object(sup, "ADMIT_WAIT_SECONDS", 0.05), \
             patch.object(sup, "container_headroom_mb", return_value=106.0):
            # Fits: a measured-cheap connector.
            with patch.object(sup, "DEFAULT_CONNECTOR_MB", 70.0):
                async_to_sync(self._admit)((1, 1), 1)
            # Does not: the expensive one, though the *fraction* is unchanged.
            with patch.object(sup, "DEFAULT_CONNECTOR_MB", 150.0):
                with self.assertRaises(ConnectorBudgetExceeded):
                    async_to_sync(self._admit)((2, 1), 2)

    def test_a_zero_budget_disables_the_ceiling(self):
        """Local development, and any host with room, is unaffected."""
        with patch.object(sup, "MEMORY_BUDGET_MB", 0.0), \
             patch.object(sup, "DEFAULT_CONNECTOR_MB", 500.0), \
             patch.object(sup, "MAX_CONCURRENT_STARTS", 4):
            for i in range(4):
                async_to_sync(self._admit)((i, 1), i)

    def test_a_measured_connector_replaces_the_default_estimate(self):
        """The budget reasons in measured megabytes once it has seen one."""
        with patch.object(sup, "proc_available", return_value=True), \
             patch.object(sup, "tree_rss_mb", return_value=25.0):
            self.sup.register((1, 1), 7, [4242])
        self.assertEqual(self.sup.estimate_for(7), 25.0)

    def test_release_kills_what_the_transport_left_behind(self):
        with patch.object(sup, "proc_available", return_value=True), \
             patch.object(sup, "tree_rss_mb", return_value=25.0), \
             patch.object(sup, "kill_tree", return_value=1) as kill:
            self.sup.register((1, 1), 7, [4242])
            self.sup.release((1, 1))
        kill.assert_called_once()
        self.assertEqual(set(kill.call_args[0][0]), {4242})


class ProcessMeasurementTests(SimpleTestCase):
    """The measuring itself, on hosts that have `/proc`."""

    def setUp(self) -> None:
        if not os.path.isdir("/proc/self"):
            self.skipTest("no /proc on this host")

    def test_our_own_process_reports_some_memory(self):
        self.assertGreater(sup.tree_rss_mb([os.getpid()]), 0.0)

    def test_kill_tree_ignores_pids_that_are_not_ours(self):
        """Pids are reused: a stale one can belong to anything by now."""
        with patch.object(sup, "descendants", return_value=set()):
            self.assertEqual(sup.kill_tree([1]), 0)
