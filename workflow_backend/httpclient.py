"""
One outbound HTTP client per event loop, so provider calls reuse connections.

Every model call used to open `async with httpx.AsyncClient(...)` and close it
on the way out, which threw away the connection pool the client exists to
provide: each call paid a fresh DNS lookup, TCP connect and TLS handshake to the
same provider it had just spoken to. An agent run makes a model call per
iteration — up to forty — plus a tool's worth of Google API calls between them,
so that handshake was a fixed tax on every step. httpx's own guidance is the
opposite: one long-lived client, not one per request inside a hot loop.

Why per *loop* rather than one module global: an `AsyncClient`'s pooled
connections belong to the loop that opened them. Production has one loop
(daphne), but management commands, Celery tasks and tests each run
`asyncio.run`, and a connection reused from a finished loop fails on first use.
The map is weak, so a loop that is gone takes its client with it.

Callers pass `timeout=` per request instead of on the client, because the
budgets genuinely differ (a streamed agent turn vs a Drive listing), and must
never use `async with shared_client()` — that closes the client for everyone.
"""
from __future__ import annotations

import asyncio
import weakref

import httpx

#: Applies only where a caller forgets its own `timeout=`; every call site in
#: the codebase passes one.
DEFAULT_TIMEOUT = httpx.Timeout(60.0, connect=10.0)

#: Keep-alive is the point, so idle connections outlive a model's think time
#: between iterations (httpx's default expiry is 5 s, shorter than most turns).
LIMITS = httpx.Limits(
    max_connections=100,
    max_keepalive_connections=20,
    keepalive_expiry=60.0,
)

_clients: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, httpx.AsyncClient] = (
    weakref.WeakKeyDictionary()
)


def shared_client() -> httpx.AsyncClient:
    """The pooled client for the running event loop, created on first use."""
    loop = asyncio.get_running_loop()
    client = _clients.get(loop)
    if client is None or client.is_closed:
        client = httpx.AsyncClient(timeout=DEFAULT_TIMEOUT, limits=LIMITS)
        _clients[loop] = client
    return client
