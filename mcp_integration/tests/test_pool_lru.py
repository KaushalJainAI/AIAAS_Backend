"""The session pool is bounded, and bounded by *use* rather than by age.

`_pool` had a TTL and no size cap, so the number of live stdio subprocesses was
decided by how many connectors people happened to touch rather than by
configuration. Each one is a Node process on a RAM-tight box, which makes that
the shape of an OOM rather than of a cache.

Two properties are load-bearing here and neither is obvious from the code:

* eviction is by **least recently used**, not least recently created — evicting
  by age throws out the connector every turn is calling and keeps one touched
  once an hour ago; and
* a session someone is **mid-call on is never evicted**, so the cap is soft.
  Sitting one over it for the length of a call beats breaking the call.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from asgiref.sync import async_to_sync
from django.test import SimpleTestCase

from mcp_integration import client as mcp_client
from mcp_integration.client import _pool, _touch, _trim_pool
from mcp_integration import supervisor as sup
from mcp_integration.supervisor import supervisor


def _entry(*, expired: bool = False, alive: bool = True) -> MagicMock:
    """A pool entry with a real lock, since `_trim_pool` reads `lock.locked()`."""
    entry = MagicMock()
    entry.lock = asyncio.Lock()
    entry.expired.return_value = expired
    entry.alive.return_value = alive
    entry.worker = AsyncMock()
    return entry


class PoolLRUTests(SimpleTestCase):
    def setUp(self) -> None:
        _pool.clear()
        self.addCleanup(_pool.clear)
        # Clearing the pool directly bypasses eviction, so the memory
        # supervisor would keep accounting for sessions this test never
        # opened — and refuse the starts the case is about. Same trap as
        # `CredentialManager`'s process-global cache.
        supervisor.reset()
        self.addCleanup(supervisor.reset)

    @staticmethod
    async def _trim(protect=None) -> None:
        """Trim, then let the detached closes run.

        `_trim_pool` hands each victim to `spawn` rather than awaiting it: a
        close waits up to `CLOSE_TIMEOUT` for a worker to unwind, and this runs
        while a caller is waiting to acquire a session.
        """
        _trim_pool(protect=protect)
        await asyncio.sleep(0)

    def test_the_pool_never_exceeds_its_cap(self):
        for i in range(9):
            _pool[(i, 1)] = _entry()

        with patch.object(mcp_client, "MAX_POOLED_SESSIONS", 4):
            async_to_sync(self._trim)()

        self.assertEqual(len(_pool), 4)

    def test_eviction_is_by_last_use_not_by_age(self):
        """The whole reason recency is tracked where a session is borrowed.

        Insertion order alone would evict `a` here — the oldest — which is
        precisely the connector being used on every turn.
        """
        for name in ("a", "b", "c"):
            _pool[(name, 1)] = _entry()

        _touch(("a", 1))  # a is now the most recently used

        with patch.object(mcp_client, "MAX_POOLED_SESSIONS", 2):
            async_to_sync(self._trim)()

        self.assertIn(("a", 1), _pool)
        self.assertIn(("c", 1), _pool)
        self.assertNotIn(("b", 1), _pool)

    def test_a_session_in_use_is_never_evicted(self):
        """The cap is soft on purpose: breaking a live call is the worse bug."""

        async def scenario():
            busy = _entry()
            await busy.lock.acquire()  # someone is mid-call on this one
            _pool[("busy", 1)] = busy
            for i in range(5):
                _pool[(i, 1)] = _entry()

            with patch.object(mcp_client, "MAX_POOLED_SESSIONS", 2):
                await self._trim()

            # `busy` is the least recently used *and* survives.
            self.assertIn(("busy", 1), _pool)
            busy.worker.close.assert_not_awaited()
            busy.lock.release()

        async_to_sync(scenario)()

    def test_an_evicted_session_is_actually_closed(self):
        victim = _entry()
        _pool[("victim", 1)] = victim
        _pool[("keep", 1)] = _entry()

        with patch.object(mcp_client, "MAX_POOLED_SESSIONS", 1):
            async_to_sync(self._trim)()

        self.assertNotIn(("victim", 1), _pool)
        victim.worker.close.assert_awaited_once()

    def test_an_expired_session_goes_even_under_the_cap(self):
        """The TTL has to keep meaning something once a cap exists.

        It was only ever enforced when somebody asked for the same key again,
        so an idle connector on a quiet box held its subprocess indefinitely.
        The cap alone would not have fixed that — nothing is over it here.
        """
        stale = _entry(expired=True)
        _pool[("stale", 1)] = stale
        _pool[("fresh", 1)] = _entry()

        with patch.object(mcp_client, "MAX_POOLED_SESSIONS", 50):
            async_to_sync(self._trim)()

        self.assertNotIn(("stale", 1), _pool)
        self.assertIn(("fresh", 1), _pool)
        stale.worker.close.assert_awaited_once()

    def test_a_dead_session_is_reaped_too(self):
        dead = _entry(alive=False)
        _pool[("dead", 1)] = dead

        with patch.object(mcp_client, "MAX_POOLED_SESSIONS", 50):
            async_to_sync(self._trim)()

        self.assertNotIn(("dead", 1), _pool)

    def test_the_entry_just_created_is_never_the_victim(self):
        """`_trim_pool` runs right after an insert, so it must protect it.

        Without this a cold connector could be evicted by its own arrival when
        the pool is full of unlocked entries, and the caller would immediately
        hit the "session was evicted; please retry" path it just paid an `npx`
        start to avoid.
        """
        for i in range(5):
            _pool[(i, 1)] = _entry()
        newest = _entry()
        _pool[("newest", 1)] = newest

        with patch.object(mcp_client, "MAX_POOLED_SESSIONS", 1):
            async_to_sync(self._trim)(("newest", 1))

        self.assertIn(("newest", 1), _pool)
        newest.worker.close.assert_not_awaited()

    def test_a_zero_cap_disables_the_size_bound_but_not_the_reaper(self):
        """0 means "no size limit", which is how an operator turns the cap off.

        Expiry still applies: the two are separate questions and switching off
        the ceiling must not also switch off the TTL.
        """
        stale = _entry(expired=True)
        _pool[("stale", 1)] = stale
        for i in range(5):
            _pool[(i, 1)] = _entry()

        with patch.object(mcp_client, "MAX_POOLED_SESSIONS", 0):
            async_to_sync(self._trim)()

        self.assertNotIn(("stale", 1), _pool)
        self.assertEqual(len(_pool), 5)


class PoolBoundIsWiredIntoAcquisitionTests(SimpleTestCase):
    """The cap has to hold on the path that actually opens sessions.

    The cases above drive `_trim_pool` directly, which proves the policy and
    proves nothing about whether `_session` ever calls it. That is the shape of
    bug this codebase has been bitten by before — every hop tested, the chain
    connected nowhere — so this one goes through `MCPClientManager._session`
    with only the transport faked.
    """

    def setUp(self) -> None:
        _pool.clear()
        self.addCleanup(_pool.clear)
        # Clearing the pool directly bypasses eviction, so the memory
        # supervisor would keep accounting for sessions this test never
        # opened — and refuse the starts the case is about. Same trap as
        # `CredentialManager`'s process-global cache.
        supervisor.reset()
        self.addCleanup(supervisor.reset)
        # These cases are about the *count* cap and its LRU order. The memory
        # budget is a second, independent ceiling (test_supervisor.py) and on
        # this catalogue it is the tighter of the two — a 150 MB budget against
        # a 110 MB estimate admits exactly one connector, so leaving it on
        # would evict for reasons that have nothing to do with what is under
        # test here.
        budget = patch.object(sup, "MEMORY_BUDGET_MB", 0.0)
        budget.start()
        self.addCleanup(budget.stop)
        headroom = patch.object(sup, "container_headroom_mb", return_value=None)
        headroom.start()
        self.addCleanup(headroom.stop)

    def test_opening_more_sessions_than_the_cap_evicts_as_it_goes(self):
        from types import SimpleNamespace

        from mcp_integration.client import MCPClientManager

        class _FakeWorker:
            """A worker that reports a live session without a transport.

            `_PoolEntry.alive()` reads `worker.session` and `worker.task`, and a
            `None` task reads as dead — which `_trim_pool` would reap on sight,
            hiding the very behaviour under test. Hence a real pending task.
            """

            def __init__(self, manager, server, resolved, key=None):
                self.session = SimpleNamespace(name="session")
                self.task = None
                # The supervisor attributes subprocesses to a session and
                # releases them on close; a double with no transport has none.
                self.pids = set()
                self._stop = asyncio.Event()

            async def start(self):
                self.task = asyncio.ensure_future(self._stop.wait())

            async def close(self):
                self._stop.set()
                if self.task is not None:
                    await self.task

        async def scenario():
            resolved = SimpleNamespace(env_vars={}, headers={})
            with patch.object(mcp_client, "_SessionWorker", _FakeWorker), \
                 patch.object(mcp_client, "MAX_POOLED_SESSIONS", 3):
                for server_id in range(8):
                    server = SimpleNamespace(id=server_id, name=f"s{server_id}",
                                             type="stdio")
                    manager = MCPClientManager(server_id, user=1)
                    async with manager._session(server, resolved):
                        pass
                    # Nothing is borrowed between iterations, so the cap is
                    # hard here rather than soft.
                    self.assertLessEqual(len(_pool), 3)

            await asyncio.sleep(0)

        async_to_sync(scenario)()
        self.assertLessEqual(len(_pool), 3)

    def test_a_reused_session_is_not_the_next_victim(self):
        """`_touch` fires where the session is borrowed, not where it is found.

        Without that, the connector being used on every turn is the oldest
        entry in the pool and therefore the first thing evicted.
        """
        from types import SimpleNamespace

        from mcp_integration.client import MCPClientManager

        class _FakeWorker:
            def __init__(self, manager, server, resolved, key=None):
                self.session = SimpleNamespace(name="session")
                self.task = None
                # The supervisor attributes subprocesses to a session and
                # releases them on close; a double with no transport has none.
                self.pids = set()
                self._stop = asyncio.Event()

            async def start(self):
                self.task = asyncio.ensure_future(self._stop.wait())

            async def close(self):
                self._stop.set()
                if self.task is not None:
                    await self.task

        async def scenario():
            resolved = SimpleNamespace(env_vars={}, headers={})
            with patch.object(mcp_client, "_SessionWorker", _FakeWorker), \
                 patch.object(mcp_client, "MAX_POOLED_SESSIONS", 2):
                async def use(server_id):
                    server = SimpleNamespace(id=server_id, name=f"s{server_id}",
                                             type="stdio")
                    async with MCPClientManager(server_id, user=1)._session(
                        server, resolved
                    ):
                        pass

                await use(0)
                await use(1)
                await use(0)  # server 0 is now the most recently used
                await use(2)  # forces an eviction

            await asyncio.sleep(0)
            # 1 was least recently used and goes; 0 survives because it was
            # touched on its second borrow.
            self.assertIn((0, 1), _pool)
            self.assertNotIn((1, 1), _pool)

        async_to_sync(scenario)()
