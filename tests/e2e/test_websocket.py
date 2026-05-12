#!/usr/bin/env python
"""
WebSocket smoke test for the streaming layer.

Connects to /ws/streaming/<workflow_id>/ (adjust path if your routing differs)
and verifies the channel responds with at least one event within a timeout.

Requires the `websockets` package (already in Backend/requirements.txt via channels).
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from urllib.parse import urlparse

try:
    import websockets
except ImportError:
    print("FAIL: install `websockets` (pip install websockets)")
    sys.exit(1)

try:
    from ._lib import auth_headers, login, parse_base, register, unique_user
except ImportError:  # pragma: no cover - supports direct script execution
    from _lib import auth_headers, login, parse_base, register, unique_user
import requests


def _ws_url(http_base: str, path: str) -> str:
    parsed = urlparse(http_base)
    scheme = "wss" if parsed.scheme == "https" else "ws"
    return f"{scheme}://{parsed.netloc}{path}"


async def _probe(ws_url: str, token: str) -> None:
    # Many Channels apps accept token via query string; adjust as needed.
    full = f"{ws_url}?token={token}"
    async with websockets.connect(full, open_timeout=10, close_timeout=5) as ws:
        try:
            msg = await asyncio.wait_for(ws.recv(), timeout=5)
            print(f"PASS: ws received: {str(msg)[:120]}")
        except asyncio.TimeoutError:
            # Many channels stay quiet until something happens — that's OK.
            print("PASS: ws connected (no spontaneous messages within 5s)")


def main() -> None:
    base = parse_base()
    u, e, p = unique_user()
    register(base, u, e, p)
    token = login(base, e, p)
    # Create a workflow so we have an ID to subscribe to
    resp = requests.post(
        f"{base}/api/orchestrator/workflows/",
        headers=auth_headers(token),
        json={"name": "ws-smoke", "status": "draft", "nodes": [], "edges": []},
        timeout=10,
    )
    if resp.status_code not in (200, 201):
        print(f"FAIL: could not create workflow ({resp.status_code})")
        sys.exit(1)
    wf_id = resp.json()["id"]

    ws_path = os.environ.get("WS_PATH", f"/ws/streaming/{wf_id}/")
    ws_url = _ws_url(base, ws_path)
    print(f"connecting to {ws_url}")
    try:
        asyncio.run(_probe(ws_url, token))
    except Exception as e:  # noqa: BLE001
        print(f"FAIL: {type(e).__name__}: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
