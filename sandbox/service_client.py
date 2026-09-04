"""Backend-side client for the sandbox sidecar (`sandbox_service/`).

The sidecar is a separate hardened container reachable only over the internal
docker network. This module POSTs a snippet to it and normalizes the reply into
the same envelope the in-process engine returns, so `sandbox.engine` can treat
the two interchangeably.

A network or service failure is reported as a failed run, never as a silent
fall-through to the weaker in-process engine — downgrading the sandbox because
a container is momentarily unreachable is exactly the kind of quiet security
regression this whole change exists to remove. The engine choice is explicit
config; only config changes it.
"""
from __future__ import annotations

import logging

import httpx
from django.conf import settings

logger = logging.getLogger(__name__)


def _service_url() -> str:
    return getattr(settings, "SANDBOX_SERVICE_URL", "http://sandbox:8100").rstrip("/")


async def run_via_service(code: str) -> dict:
    """Execute `code` on the sidecar. Returns the normalized envelope."""
    wall = int(getattr(settings, "SANDBOX_WALL_SECONDS", 10))
    cpu = int(getattr(settings, "SANDBOX_CPU_SECONDS", 8))
    mem_mb = int(getattr(settings, "SANDBOX_MEM_MB", 384))

    payload = {"code": code, "wall_seconds": wall, "cpu_seconds": cpu, "mem_mb": mem_mb}
    # The HTTP read timeout sits above the sandbox's own wall-clock so the
    # in-container kill is what ends a runaway run, not a dropped connection
    # that leaves the subprocess orphaned.
    http_timeout = wall + 10

    try:
        async with httpx.AsyncClient(timeout=http_timeout) as client:
            resp = await client.post(f"{_service_url()}/execute", json=payload)
            resp.raise_for_status()
            env = resp.json()
    except httpx.TimeoutException:
        return _fail("The sandbox did not respond in time.")
    except httpx.HTTPStatusError as exc:
        detail = "sandbox is busy" if exc.response.status_code == 503 else "sandbox rejected the request"
        return _fail(f"Sandbox error: {detail}.")
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("[sandbox] service call failed: %s", exc)
        return _fail("The code sandbox is unavailable right now.")

    # Trust the shape but backfill anything an older sidecar might omit.
    if not isinstance(env, dict):
        return _fail("The sandbox returned an unexpected response.")
    env.setdefault("success", False)
    env.setdefault("result", None)
    env.setdefault("output", "")
    env.setdefault("stderr", "")
    env.setdefault("error", None)
    env.setdefault("timed_out", False)
    return env


def _fail(message: str) -> dict:
    return {
        "success": False,
        "result": None,
        "output": "",
        "stderr": "",
        "error": message,
        "timed_out": False,
    }
