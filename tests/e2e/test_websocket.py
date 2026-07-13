#!/usr/bin/env python
"""
WebSocket smoke test for the streaming layer.

Real flow: register → login → create gemini credential → create workflow with
manual_trigger → gemini → POST /execute/ → connect to /ws/execution/{id}/ and
verify at least one streamed event arrives.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from urllib.parse import urlparse

try:
    import websockets
except ImportError:
    print("FAIL: install `websockets` (pip install websockets)")
    sys.exit(1)

try:
    from ._lib import (
        auth_headers,
        create_nvidia_credential,
        load_env_file,
        login,
        minimal_nvidia_workflow,
        parse_base,
        register,
        unique_user,
    )
except ImportError:  # pragma: no cover - direct script execution
    from _lib import (
        auth_headers,
        create_nvidia_credential,
        load_env_file,
        login,
        minimal_nvidia_workflow,
        parse_base,
        register,
        unique_user,
    )
import requests


def _ws_url(http_base: str, path: str) -> str:
    parsed = urlparse(http_base)
    scheme = "wss" if parsed.scheme == "https" else "ws"
    return f"{scheme}://{parsed.netloc}{path}"


async def _probe(ws_url: str, token: str, timeout_s: float = 30.0) -> int:
    """Connect, collect frames until timeout or status==completed. Returns frame count."""
    full = f"{ws_url}?token={token}"
    frames = 0
    async with websockets.connect(full, open_timeout=10, close_timeout=5) as ws:
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            remaining = max(0.5, deadline - time.time())
            try:
                msg = await asyncio.wait_for(ws.recv(), timeout=remaining)
            except asyncio.TimeoutError:
                break
            frames += 1
            preview = str(msg)[:160]
            print(f"  ws frame #{frames}: {preview}")
            try:
                parsed = json.loads(msg) if isinstance(msg, (str, bytes)) else {}
            except json.JSONDecodeError:
                parsed = {}
            evt = (parsed.get("type") or parsed.get("event") or "").lower()
            state = (parsed.get("state") or parsed.get("status") or "").lower()
            if evt in {"execution_completed", "completed", "finished"} or state in {"completed", "succeeded", "finished"}:
                break
    return frames


def main() -> None:
    base = parse_base()
    load_env_file()
    api_key = os.environ.get("NVIDIA_API_KEY")
    if not api_key:
        print("FAIL: NVIDIA_API_KEY not set in env or Backend/.env")
        sys.exit(1)

    u, e, p = unique_user()
    register(base, u, e, p)
    token = login(base, e, p)

    cred_id = create_nvidia_credential(base, token, api_key, name=f"e2e-ws-{int(time.time())}")
    wf = minimal_nvidia_workflow(cred_id)
    resp = requests.post(
        f"{base}/api/orchestrator/workflows/",
        headers=auth_headers(token),
        json=wf,
        timeout=15,
    )
    if resp.status_code not in (200, 201):
        print(f"FAIL: workflow create {resp.status_code}: {resp.text[:300]}")
        sys.exit(1)
    wf_id = resp.json()["id"]

    resp = requests.post(
        f"{base}/api/orchestrator/workflows/{wf_id}/execute/",
        headers=auth_headers(token),
        json={
            "input_data": {},
            "llm_provider": "nvidia",
            "llm_model": "nvidia/llama-3.3-nemotron-super-49b-v1",
            "llm_credential": cred_id,
        },
        timeout=30,
    )
    if resp.status_code != 200:
        print(f"FAIL: execute {resp.status_code}: {resp.text[:300]}")
        sys.exit(1)
    exec_id = resp.json().get("execution_id")
    if not exec_id:
        print(f"FAIL: missing execution_id in {resp.json()}")
        sys.exit(1)
    print(f"started execution {exec_id}")

    ws_path = os.environ.get("WS_PATH", f"/ws/execution/{exec_id}/")
    ws_url = _ws_url(base, ws_path)
    print(f"connecting to {ws_url}")
    try:
        frames = asyncio.run(_probe(ws_url, token, timeout_s=45.0))
    except Exception as e:  # noqa: BLE001
        print(f"FAIL: {type(e).__name__}: {e}")
        sys.exit(1)

    if frames == 0:
        print("FAIL: no WS frames received within 45s")
        sys.exit(1)
    print(f"PASS: received {frames} ws frame(s) for execution {exec_id}")


if __name__ == "__main__":
    main()
