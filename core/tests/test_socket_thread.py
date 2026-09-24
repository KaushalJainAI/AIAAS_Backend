"""
Phase 3 (`docs/CONCURRENCY_LAG_FIX_PLAN.md`): each socket gets its own thread.

Channels runs consumers in no `ThreadSensitiveContext`, so every
`database_sync_to_async` from every socket falls through to asgiref's
process-wide single thread. The proof below runs two sockets at once through
`SocketThreadConsumer` and shows their thread-sensitive work lands on two
threads — with the plain consumer as the baseline showing one, which is the
bug the base exists to fix.
"""
from __future__ import annotations

import asyncio
import contextvars
import threading

from asgiref.sync import sync_to_async
from channels.generic.websocket import AsyncWebsocketConsumer
from django.test import TestCase

from core.realtime.consumers import SocketThreadConsumer


async def _run_socket(consumer_cls, idents: list) -> None:
    """Drive one connection: connect, record the DB thread, disconnect.

    Each socket runs in a fresh `contextvars.Context()`, as production gives
    each connection: the test runner itself sits under asgiref's
    `CurrentThreadExecutor`, which thread-sensitive `sync_to_async` prefers
    over any `ThreadSensitiveContext` — the same inheritance `spawn` exists
    to escape (`workflow_backend.background`).
    """

    class Probe(consumer_cls):
        async def connect(self):
            await self.accept()
            idents.append(await sync_to_async(threading.get_ident)())

    messages = [
        {"type": "websocket.connect"},
        {"type": "websocket.disconnect", "code": 1000},
    ]

    async def receive():
        return messages.pop(0)

    async def send(_message):
        pass

    async def drive():
        await Probe()({"type": "websocket", "path": "/"}, receive, send)

    loop = asyncio.get_running_loop()
    await loop.create_task(drive(), context=contextvars.Context())


class SocketThreadTests(TestCase):
    """`TestCase`, not `SimpleTestCase`: Channels' `dispatch` runs
    `aclose_old_connections` after every message, which touches the
    connection hard enough to trip Django's DB blocker when an earlier suite
    left one open on the shared thread. These tests assert threads, not rows.
    """
    async def test_two_sockets_get_different_threads(self):
        idents: list = []
        await asyncio.gather(
            _run_socket(SocketThreadConsumer, idents),
            _run_socket(SocketThreadConsumer, idents),
        )
        self.assertEqual(len(idents), 2)
        self.assertNotEqual(idents[0], idents[1])

    async def test_plain_consumer_shares_one_thread(self):
        """Baseline: without the base, both sockets queue on the one global
        thread. If this ever stops holding, asgiref changed and the base may
        be redundant (same shape as the `spawn` baseline test)."""
        idents: list = []
        await asyncio.gather(
            _run_socket(AsyncWebsocketConsumer, idents),
            _run_socket(AsyncWebsocketConsumer, idents),
        )
        self.assertEqual(len(idents), 2)
        self.assertEqual(idents[0], idents[1])
