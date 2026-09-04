"""One door to code execution, whichever engine is configured.

Two engines exist and they are chosen by config, never by accident:

- ``service`` — the hardened sidecar container (`sandbox_service/`). Real
  kernel-level confinement, and C extensions (numpy/pandas) work. This is the
  production path; docker-compose sets `SANDBOX_ENGINE=service`.
- ``inprocess`` — the AST-guarded `exec`-on-a-thread engine in
  `safe_execution.py`. It is the *local-dev fallback only* (Windows has none of
  the POSIX primitives the sidecar needs), and it is explicitly the weaker of
  the two: an AST denylist is not a real isolation boundary. Nothing in
  production should select it.

`SANDBOX_ENGINE` decides. It defaults to ``inprocess`` so a bare `manage.py
runserver` with no sidecar still works; the deployed image sets it to
``service``. There is deliberately **no automatic fallback** from service to
in-process — if the sidecar is down, a run fails loudly rather than quietly
dropping to the weaker engine.

Both engines return the same envelope: ``success``, ``result``, ``output``,
``stderr``, ``error``, ``timed_out``.
"""
from __future__ import annotations

from django.conf import settings


def _engine() -> str:
    return getattr(settings, "SANDBOX_ENGINE", "inprocess")


async def arun_code(code: str) -> dict:
    """Execute `code` through the configured engine. Async."""
    if _engine() == "service":
        from .service_client import run_via_service
        return await run_via_service(code)

    from asgiref.sync import sync_to_async
    from .safe_execution import get_sandbox

    # The in-process engine joins a worker thread; keep the event loop free.
    # Not thread_sensitive: it touches no ORM and must not queue behind the
    # request's own executor.
    outcome = await sync_to_async(get_sandbox().execute, thread_sensitive=False)(code)
    outcome.setdefault("timed_out", False)
    return outcome
