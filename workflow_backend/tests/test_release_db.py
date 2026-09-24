"""
Phase 1 tests (`docs/CONCURRENCY_LAG_FIX_PLAN.md`): a task must not hold a
pooled DB connection across a wait it does not control.

The pool tests need a real Postgres pool, so they skip on SQLite — which is
every local run. They run in the one-off Postgres pass the plan calls for
alongside Phase 2. `ReleaseDbWithoutPool` runs everywhere: `release_db` must
be harmless where there is no pool, because every dev turn calls it.
"""
from __future__ import annotations

import asyncio
import unittest

from django.test import TestCase


def _pool():
    """The Django psycopg pool, or `None` when pooling is off (SQLite dev)."""
    try:
        from django.db import connection

        return connection.pool
    except Exception:  # noqa: BLE001 — exotic settings mean "no pool"
        return None


POOL_ON = _pool() is not None


@unittest.skipUnless(POOL_ON, "needs a Postgres pool (the Phase 2 pass)")
class ReleasedWhileBlocked(TestCase):
    """A stubbed turn holds a connection for its ORM, then none for its wait."""

    async def test_blocked_turn_holds_no_connection(self):
        from asgiref.sync import sync_to_async
        from django.contrib.auth import get_user_model
        from django.db import connection

        from workflow_backend.background import release_db

        pool = connection.pool
        baseline = pool.get_stats()["pool_available"]

        opened = asyncio.Event()
        allow_release = asyncio.Event()
        released = asyncio.Event()
        finish = asyncio.Event()

        async def stubbed_turn():
            # Pre-model ORM, as `run_chat_turn` does before `agent_node`.
            await sync_to_async(lambda: get_user_model().objects.count())()
            opened.set()
            await allow_release.wait()
            # What `agent_node` now does before the model call.
            await release_db()
            released.set()
            # The model call: a wait on something that is not the database.
            await finish.wait()
            # Post-call ORM re-takes a connection in microseconds.
            return await sync_to_async(lambda: get_user_model().objects.count())()

        task = asyncio.ensure_future(stubbed_turn())
        try:
            await asyncio.wait_for(opened.wait(), timeout=10)
            self.assertEqual(
                pool.get_stats()["pool_available"], baseline - 1,
                "the turn holds one connection for its ORM",
            )
            allow_release.set()
            await asyncio.wait_for(released.wait(), timeout=10)
            self.assertEqual(
                pool.get_stats()["pool_available"], baseline,
                "nothing held while the model call is blocked",
            )
            finish.set()
            self.assertIsInstance(await asyncio.wait_for(task, timeout=10), int)
        finally:
            if not task.done():
                task.cancel()

    async def test_fifteen_concurrent_turns_never_wait_for_a_connection(self):
        """15 in-flight turns, all blocked at once, hold nothing between them.

        The plan names `max_size=4` for this; the pool here is whatever the
        Postgres pass configures, so the test asserts the property instead of
        the number: while every turn is blocked, nothing is held, and all 15
        complete without ever nearing `DB_POOL_TIMEOUT`.
        """
        from asgiref.sync import sync_to_async
        from django.contrib.auth import get_user_model
        from django.db import connection

        from workflow_backend.background import release_db

        pool = connection.pool
        baseline = pool.get_stats()["pool_available"]
        entered = [asyncio.Event() for _ in range(15)]
        gate = asyncio.Event()

        async def stubbed_turn(i: int):
            await sync_to_async(lambda: get_user_model().objects.count())()
            await release_db()
            entered[i].set()
            await gate.wait()
            return await sync_to_async(lambda: get_user_model().objects.count())()

        tasks = [asyncio.ensure_future(stubbed_turn(i)) for i in range(15)]
        try:
            await asyncio.wait_for(
                asyncio.gather(*(e.wait() for e in entered)), timeout=30,
            )
            self.assertEqual(
                pool.get_stats()["pool_available"], baseline,
                "15 blocked turns hold no connections between them",
            )
            gate.set()
            results = await asyncio.wait_for(asyncio.gather(*tasks), timeout=30)
            self.assertEqual(len(results), 15)
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()


class ReleaseDbWithoutPool(TestCase):
    """On SQLite there is no pool; `release_db` must be a harmless no-op."""

    async def test_release_then_query(self):
        from asgiref.sync import sync_to_async
        from django.contrib.auth import get_user_model

        from workflow_backend.background import release_db

        await release_db()
        self.assertIsInstance(
            await sync_to_async(lambda: get_user_model().objects.count())(), int,
        )

    async def test_release_is_idempotent(self):
        from workflow_backend.background import release_db

        await release_db()
        await release_db()
