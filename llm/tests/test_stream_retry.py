"""
A provider stream that cannot connect is retried — only before anything was
emitted, and never for an HTTP error the provider actually answered with.

Found by the work-tier stress benchmark (2026-09-17): one `httpx.ConnectError`
on the first model call failed two whole agent runs, reported as the bare
"OpenRouter error: " because that exception stringifies to "".
"""
from unittest.mock import patch

import httpx
from asgiref.sync import async_to_sync
from django.test import SimpleTestCase

from llm.handlers import openai_compatible
from llm.handlers.llm_providers import OpenRouterNode


class _Response:
    def __init__(self, status: int, body: bytes = b'{"error": "upstream"}'):
        self.status_code = status
        self._body = body

    async def aread(self):
        return self._body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _Client:
    """Raises `error` for the first `failures` calls, then answers with `status`."""

    def __init__(self, failures: int, status: int = 500, error=None):
        self.failures, self.status, self.calls = failures, status, 0
        self.error = error or httpx.ConnectError('')

    def stream(self, *args, **kwargs):
        self.calls += 1
        if self.calls <= self.failures:
            raise self.error
        return _Response(self.status)


class StreamRetryTests(SimpleTestCase):
    def events(self, client):
        async def key(*_a, **_k):
            return 'sk-test'

        async def messages(*_a, **_k):
            return [{'role': 'user', 'content': 'hi'}]

        async def no_sleep(_seconds):
            return None

        node = OpenRouterNode()

        async def collect():
            return [e async for e in node.stream_execute({}, {'prompt': 'hi', 'model': 'x/y'}, None)]

        with patch.object(openai_compatible, 'shared_client', lambda: client), \
                patch.object(openai_compatible, 'resolve_node_api_key', key), \
                patch.object(OpenRouterNode, '_messages', messages), \
                patch.object(openai_compatible.asyncio, 'sleep', no_sleep):
            return async_to_sync(collect)()

    def test_a_connection_blip_is_retried_until_the_provider_answers(self):
        client = _Client(failures=2, status=500)
        events = self.events(client)
        self.assertEqual(client.calls, 3)
        # The provider's own answer comes through, status and all.
        self.assertEqual(events[-1]['status'], 500)

    def test_a_dead_connection_gives_up_and_names_the_failure(self):
        client = _Client(failures=99)
        events = self.events(client)
        self.assertEqual(client.calls, len(openai_compatible.STREAM_CONNECT_RETRY_DELAYS) + 1)
        self.assertEqual(events, [{'type': 'error', 'message': 'OpenRouter error: ConnectError'}])

    def test_a_timeout_before_any_output_is_retried_too(self):
        client = _Client(failures=1, status=200, error=httpx.ReadTimeout('timed out'))
        self.events(client)
        self.assertEqual(client.calls, 2)

    def test_a_timeout_that_never_clears_is_reported_as_a_timeout(self):
        client = _Client(failures=99, error=httpx.ReadTimeout('timed out'))
        events = self.events(client)
        self.assertEqual(client.calls, len(openai_compatible.STREAM_CONNECT_RETRY_DELAYS) + 1)
        self.assertEqual(events, [{'type': 'error', 'message': 'OpenRouter API request timed out'}])

    def test_an_http_error_is_never_retried(self):
        client = _Client(failures=0, status=401)
        self.events(client)
        self.assertEqual(client.calls, 1)
