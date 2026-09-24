"""The sidecar's concurrency cap (Phase 5 of CONCURRENCY_LAG_FIX_PLAN.md).

Past its slots (`SANDBOX_MAX_CONCURRENCY`, 2 in prod) a burst must get a fast
503 — never another subprocess on a box that cannot hold it. The backend turns
that 503 into a retryable "sandbox is busy" tool error, so the model waits
and retries rather than the box swapping.

Driven over real HTTP against the real `Handler`, with the only slot held
the way a running execution would hold it. No code ever executes here, so
this needs none of the POSIX primitives — the 503 returns before `execute`.
"""
from __future__ import annotations

import importlib
import json
import os
import sys
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from unittest import mock

_SVC_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SVC_DIR not in sys.path:
    sys.path.insert(0, _SVC_DIR)


def _post(port: int, payload: dict) -> tuple[int, dict]:
    req = urllib.request.Request(
        f'http://127.0.0.1:{port}/execute',
        data=json.dumps(payload).encode('utf-8'),
        headers={'Content-Type': 'application/json'},
        method='POST',
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


class ConcurrencyCapTests(unittest.TestCase):
    def test_a_held_slot_answers_503(self):
        with mock.patch.dict(os.environ, {
            'SANDBOX_MAX_CONCURRENCY': '1',
            # Bound this test's own wait: the refused request holds until
            # the acquire timeout, which is the wall ceiling.
            'SANDBOX_MAX_WALL_SECONDS': '1',
        }):
            import server
            server = importlib.reload(server)
            httpd = ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
            port = httpd.server_address[1]
            thread = threading.Thread(target=httpd.serve_forever, daemon=True)
            thread.start()
            try:
                server._slots.acquire()  # the running execution's slot
                try:
                    status, body = _post(port, {'code': 'result = 1'})
                finally:
                    server._slots.release()
            finally:
                httpd.shutdown()
                httpd.server_close()
        self.assertEqual(status, 503)
        self.assertIn('busy', body.get('error', ''))
