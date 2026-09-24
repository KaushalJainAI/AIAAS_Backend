"""How the service client reports a sidecar that says no (Phase 5).

With one sandbox slot, contention is normal: the sidecar answers 503 and the
client must turn that into a retryable tool error ("sandbox is busy"), not a
crash and not a silent fallback to the weaker engine. A 400 keeps the
sidecar's own reason, which names the fix.
"""
from __future__ import annotations

from unittest import mock

import httpx
from asgiref.sync import async_to_sync
from django.test import SimpleTestCase

from sandbox.service_client import run_via_service


class _FakeResponse:
    def __init__(self, status_code: int, payload: dict):
        self.status_code = status_code
        self._payload = payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                f'status {self.status_code}',
                request=mock.Mock(), response=self)

    def json(self):
        return self._payload


class _FakeClient:
    def __init__(self, response):
        self._response = response

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def post(self, *args, **kwargs):
        return self._response


class BusySandboxTests(SimpleTestCase):
    def _run(self, status_code: int, payload: dict) -> dict:
        client = _FakeClient(_FakeResponse(status_code, payload))
        with mock.patch('sandbox.service_client.httpx.AsyncClient',
                        return_value=client):
            return async_to_sync(run_via_service)('result = 1')

    def test_503_is_a_retryable_busy_error(self):
        outcome = self._run(503, {'error': 'sandbox busy, try again'})
        self.assertFalse(outcome['success'])
        self.assertIn('busy', outcome['error'])

    def test_400_keeps_the_sidecars_reason(self):
        outcome = self._run(400, {'error': 'collect must be a list of names'})
        self.assertFalse(outcome['success'])
        self.assertIn('collect must be a list of names', outcome['error'])
