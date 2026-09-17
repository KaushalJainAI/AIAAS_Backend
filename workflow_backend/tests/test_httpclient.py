"""The shared outbound client: one per loop, reused, never a dead one."""
import asyncio

from django.test import SimpleTestCase

from workflow_backend.httpclient import shared_client


class SharedClientTests(SimpleTestCase):
    def test_one_loop_reuses_one_client(self):
        async def twice():
            return shared_client(), shared_client()

        first, second = asyncio.run(twice())
        self.assertIs(first, second)

    def test_each_loop_gets_its_own_client(self):
        # A pooled connection belongs to the loop that opened it; handing one
        # loop's client to the next `asyncio.run` fails on first use.
        async def one():
            return shared_client()

        self.assertIsNot(asyncio.run(one()), asyncio.run(one()))

    def test_a_closed_client_is_replaced(self):
        async def close_then_get():
            client = shared_client()
            await client.aclose()
            return client, shared_client()

        closed, fresh = asyncio.run(close_then_get())
        self.assertIsNot(closed, fresh)
        self.assertFalse(fresh.is_closed)
