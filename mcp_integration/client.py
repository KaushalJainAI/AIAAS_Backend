"""
MCPClientManager — opens (and pools) connections to an MCP server, exposing
`list_tools` / `call_tool`.

Connection pooling
------------------
Opening a fresh connection on every tool call is expensive:
  * stdio  — spawns a new subprocess + MCP handshake (~100–500 ms)
  * SSE    — TCP connect + HTTP upgrade + MCP handshake

The module-level `_pool` keeps one live `ClientSession` per
(server_id, user_id) pair for up to SESSION_TTL seconds.  An asyncio.Lock
per pool-entry serialises concurrent callers so the session is never used
by two coroutines simultaneously (MCP sessions are not concurrency-safe).

Each pooled session lives in its own task (`_SessionWorker`) rather than in
whichever request happened to open it. Both transports are anyio task groups,
and a task group may only be exited by the task that entered it — so a session
opened by request A and evicted by request B could not be closed at all, and
its stdio subprocess was orphaned. Requests borrow the session; the worker owns
its lifetime.

If a session errors mid-call it is evicted so the next call gets a fresh one.
A failure to *connect* is remembered for FAILURE_TTL seconds, so a connector
that cannot start is not re-dialled once per request — spawning an npx that
takes eight seconds to fail, per click, is how a broken catalogue entry turns
into a load problem.
"""
from __future__ import annotations

import contextlib
import inspect
import logging
import os
import re
import shutil
import tempfile
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

import asyncio

from asgiref.sync import sync_to_async
from django.core.exceptions import PermissionDenied
from django.db.models import Q
from mcp import ClientSession, StdioServerParameters
from mcp.client.sse import sse_client
from mcp.client.stdio import stdio_client
from mcp.types import CallToolResult

from workflow_backend.background import spawn

from .credential_injector import CredentialInjector, ResolvedCredentials, _coerce_user_id
from .models import MCPServer, MCPServerPreference
from .tool_cache import MCPToolCache

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Connection pool
# ---------------------------------------------------------------------------

SESSION_TTL: float = 300.0  # seconds a session stays alive without activity
# `npx -y <pkg>` resolves and installs before the server prints a byte: measured
# 8.5 s for a working connector on this catalogue and 7.7 s for npm to report
# E404 on a missing one. The old 5 s budget was shorter than either, so *every*
# stdio connector timed out — a working one and a nonexistent package were
# indistinguishable, both surfacing as a bare `TimeoutError` with no message.
CONNECT_TIMEOUT: float = 25.0
LIST_TOOLS_TIMEOUT: float = 30.0
CLOSE_TIMEOUT: float = 10.0
# How long a connection failure is remembered. Without this every open of a
# broken connector's card spawns another npx that takes ~8 s to fail; a user
# clicking around a catalogue of eleven can have a dozen in flight.
FAILURE_TTL: float = 60.0
# Budgets for RPCs on an already-open session. Separate from the connect budget
# because a server that handshook and then went mute is a different failure from
# one that never started.
RPC_TIMEOUT: float = 15.0
CALL_TOOL_TIMEOUT: float = 120.0
# What an agent turn will wait for a *cold* connector before going without it.
# Deliberately far below LIST_TOOLS_TIMEOUT: a person watching a spinner on the
# Connections page can wait for npx to install, a chat turn cannot, and eleven
# connectors at the discovery budget would be half a minute of dead air.
AGENT_LIST_TOOLS_TIMEOUT: float = 8.0

_PoolKey = tuple[int, int | None]  # (server_id, user_id)


class MCPConnectionError(RuntimeError):
    """Could not open a session to the server (spawn, handshake, or transport)."""


class _SessionWorker:
    """
    Owns one MCP session inside its own task.

    Both transports are built on anyio task groups, and a task group may only
    be exited by the task that entered it. A pooled session is entered by
    whichever request opened it and closed by whichever request evicts it —
    almost never the same one — so the close raised "Attempted to exit cancel
    scope in a different task", was swallowed by `_evict`, and the stdio
    subprocess was orphaned. One leak per eviction, forever.

    Giving the session its own task makes open and close the same task by
    construction. Callers only borrow `session` under the entry lock; the
    worker parks on `_closing` in between and unwinds its own exit stack.
    """

    def __init__(self, manager: "MCPClientManager", server: "MCPServer", resolved: "ResolvedCredentials"):
        self._manager = manager
        self._server = server
        self._resolved = resolved
        self._ready = asyncio.Event()
        self._closing = asyncio.Event()
        self.session: ClientSession | None = None
        self.error: BaseException | None = None
        self.task: asyncio.Task | None = None

    async def _run(self) -> None:
        """
        The worker body. It never raises: a failure is recorded on `self.error`
        for the opener to read and the task ends normally, so a broken
        connector cannot leave an unretrieved task exception behind on a loop
        that has to keep serving every other request.
        """
        try:
            async with contextlib.AsyncExitStack() as stack:
                if self._server.type == "stdio":
                    cm = self._manager._connect_stdio(self._server, self._resolved)
                elif self._server.type == "sse":
                    cm = self._manager._connect_sse(self._server, self._resolved)
                else:
                    raise ValueError(f"Unsupported MCP server type: {self._server.type}")
                self.session = await stack.enter_async_context(cm)
                self._ready.set()
                # Hold the session open until someone evicts us. The exit stack
                # unwinds below, in this same task.
                await self._closing.wait()
        except asyncio.CancelledError:
            self.error = self.error or MCPConnectionError("session cancelled")
        except BaseException as exc:  # noqa: BLE001 — reported via self.error
            self.error = exc
            logger.warning(
                "MCP session for server %s ended: %s", self._server.id, _describe(exc)
            )
        finally:
            self.session = None
            # Whether we connected, failed, or were cancelled, the opener must
            # stop waiting or it hangs for the full connect budget.
            self._ready.set()

    async def start(self) -> None:
        """Spawn the worker and wait for the session to become usable."""
        # Detached: this task outlives the request that opened it, so it must
        # not inherit that request's asgiref executor (see background.spawn).
        self.task = spawn(self._run(), name=f"mcp-session-{self._server.id}")
        try:
            await asyncio.wait_for(self._ready.wait(), timeout=CONNECT_TIMEOUT)
        except asyncio.TimeoutError:
            await self.close()
            raise MCPConnectionError(
                f"Timed out after {CONNECT_TIMEOUT:.0f}s connecting to "
                f"'{self._server.name}'."
            ) from None
        if self.session is None:
            await self.close()
            raise MCPConnectionError(
                f"Could not connect to '{self._server.name}': {_describe(self.error)}"
            )

    async def close(self) -> None:
        """Ask the worker to unwind, and wait briefly for it to finish."""
        self._closing.set()
        task = self.task
        if task is None or task.done():
            return
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=CLOSE_TIMEOUT)
        except asyncio.TimeoutError:
            # The transport is wedged. Cancel and move on: blocking a request on
            # a dead subprocess is worse than leaving the cleanup to the
            # cancellation, which still runs in the worker's own task.
            task.cancel()
            logger.warning("MCP session for server %s did not close in time", self._server.id)
        except BaseException:  # noqa: BLE001 — closing must never raise
            logger.debug("Error awaiting MCP session close", exc_info=True)


def _describe(exc: BaseException | None) -> str:
    """
    A message that is never empty and never merely structural.

    Both transports run under anyio task groups, so a failure arrives as
    `ExceptionGroup("unhandled errors in a TaskGroup (1 sub-exception)")` —
    which says nothing at all. Flatten to the leaves, and prefer a cause over
    the wrapper, so what surfaces is the actual `FileNotFoundError` or closed
    stream rather than the shape of the plumbing that carried it.
    """
    if exc is None:
        return "no session was established"

    def leaves(e: BaseException, depth: int = 0) -> list[BaseException]:
        if depth > 4:
            return [e]
        if isinstance(e, BaseExceptionGroup):
            out: list[BaseException] = []
            for sub in e.exceptions:
                out.extend(leaves(sub, depth + 1))
            return out or [e]
        cause = e.__cause__ or e.__context__
        if not str(e).strip() and cause is not None:
            return leaves(cause, depth + 1)
        return [e]

    seen: list[str] = []
    for leaf in leaves(exc):
        text = str(leaf).strip() or leaf.__class__.__name__
        if text not in seen:
            seen.append(text)
    return "; ".join(seen) or exc.__class__.__name__


@dataclass
class _PoolEntry:
    worker: _SessionWorker
    expires_at: float
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    @property
    def session(self) -> ClientSession | None:
        return self.worker.session

    def expired(self) -> bool:
        return time.monotonic() >= self.expires_at

    def alive(self) -> bool:
        task = self.worker.task
        return self.worker.session is not None and task is not None and not task.done()

    def refresh(self) -> None:
        self.expires_at = time.monotonic() + SESSION_TTL


# Keyed by (server_id, user_id).  Entries are created lazily.
_pool: dict[_PoolKey, _PoolEntry] = {}
# One lock per key so only one coroutine creates/evicts an entry at a time.
_creation_locks: dict[_PoolKey, asyncio.Lock] = {}
# Recent connection failures, so a broken connector is not re-dialled on every
# request. Value is (expires_at, message).
_failures: dict[_PoolKey, tuple[float, str]] = {}


def _creation_lock(key: _PoolKey) -> asyncio.Lock:
    if key not in _creation_locks:
        _creation_locks[key] = asyncio.Lock()
    return _creation_locks[key]


def _recent_failure(key: _PoolKey) -> str | None:
    entry = _failures.get(key)
    if entry is None:
        return None
    expires_at, message = entry
    if time.monotonic() >= expires_at:
        _failures.pop(key, None)
        return None
    return message


def _record_failure(key: _PoolKey, message: str) -> None:
    _failures[key] = (time.monotonic() + FAILURE_TTL, message)


async def _evict(key: _PoolKey) -> None:
    """Close and remove a pool entry; safe to call when it doesn't exist."""
    entry = _pool.pop(key, None)
    if entry is not None:
        try:
            await entry.worker.close()
        except Exception:  # noqa: BLE001 — eviction must never raise
            logger.debug("Error closing MCP session for %s", key, exc_info=True)


# stderr lines that are always padding around the real message.
_NOISE = re.compile(
    r"(complete log of this run|^npm error 404$|^npm (notice|warn)|"
    r"tarball, folder, http url|Note that you can also install)",
    re.IGNORECASE,
)


class _StderrTap:
    """
    Captures a stdio server's stderr to a temp file for `stdio_client(errlog=)`.

    It has to be a real file, not a buffer: anyio hands `errlog` to the child
    process as a file descriptor, so an object without `fileno()` fails the
    spawn outright.

    Only the tail is read back. A failing `npx` writes a dozen lines of npm
    boilerplate around the one that matters, and this text ends up in an HTTP
    response, so it is bounded on both axes.
    """

    _MAX_LINES = 6
    _MAX_CHARS = 400
    _MAX_READ = 8192

    def __init__(self, server_name: str):
        self._name = server_name
        self._file = tempfile.TemporaryFile(mode="w+", encoding="utf-8", errors="replace")

    def fileno(self) -> int:
        return self._file.fileno()

    def summary(self) -> str:
        """The last few non-empty stderr lines, or '' if the child said nothing."""
        try:
            self._file.flush()
            size = os.fstat(self._file.fileno()).st_size
            self._file.seek(max(0, size - self._MAX_READ))
            text = self._file.read()
        except Exception:  # noqa: BLE001 — diagnostics must never mask the error
            return ""
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        lines = [ln for ln in lines if not _NOISE.search(ln)]
        if not lines:
            return ""
        # Keep the *first* lines, not the last: a failing tool states its
        # problem up front and then pads with recovery advice and a path to its
        # own log file, none of which helps whoever is reading this in a toast.
        return " | ".join(lines[:self._MAX_LINES])[:self._MAX_CHARS]

    def close(self) -> None:
        try:
            self._file.close()
        except Exception:  # noqa: BLE001
            pass


class MCPClientManager:
    """Connect to a single MCP server on behalf of a user."""

    def __init__(self, server_id: int, user=None):
        self.server_id = server_id
        self.user = user

    async def get_server_config(self, require_enabled: bool = True) -> MCPServer:
        """
        The server row, if this user may use it.

        `require_enabled` is relaxed only for *discovery*: asking what a
        connection can do is how a user decides whether to turn it on, so the
        capability list must work on a connection that is currently off. Every
        path that actually *runs* something keeps the default, so a disabled
        connection still cannot be called.
        """
        server = await sync_to_async(_get_visible_server_sync)(
            self.server_id,
            _coerce_user_id(self.user),
            require_enabled,
        )
        if server is None:
            raise PermissionDenied("MCP server is not available for this user.")
        return server

    async def _resolve_credentials(self, server: MCPServer) -> ResolvedCredentials:
        return await CredentialInjector.resolve(server, self.user)

    @asynccontextmanager
    async def _session(self, server: "MCPServer", resolved: "ResolvedCredentials"):
        """
        Core pool acquisition. Callers must supply already-resolved server
        config and credentials so resolution never happens more than once per
        public method call.

        Raises `MCPConnectionError` if the session cannot be opened — including
        immediately, without dialling, while a recent failure for this key is
        still remembered.
        """
        user_id = _coerce_user_id(self.user)
        key: _PoolKey = (self.server_id, user_id)

        async with _creation_lock(key):
            entry = _pool.get(key)
            if entry is not None and (entry.expired() or not entry.alive()):
                await _evict(key)
                entry = None
            if entry is None:
                cached_failure = _recent_failure(key)
                if cached_failure is not None:
                    raise MCPConnectionError(cached_failure)
                worker = _SessionWorker(self, server, resolved)
                try:
                    await worker.start()
                except MCPConnectionError as exc:
                    _record_failure(key, str(exc))
                    raise
                except BaseException:
                    # Usually the caller's own timeout cancelling us mid-connect.
                    # The worker was started with `spawn`, so it is not bound to
                    # this task's lifetime: without this it would keep a
                    # subprocess alive that nothing is left holding a handle to.
                    await worker.close()
                    raise
                entry = _PoolEntry(
                    worker=worker,
                    expires_at=time.monotonic() + SESSION_TTL,
                )
                _pool[key] = entry
                _failures.pop(key, None)

        async with entry.lock:
            if _pool.get(key) is not entry:
                raise MCPConnectionError("MCP session was evicted; please retry")
            session = entry.session
            if session is None:
                await _evict(key)
                raise MCPConnectionError(
                    f"MCP session for '{server.name}' closed unexpectedly."
                )
            try:
                entry.refresh()
                yield session
            except BaseException:
                # A session that errored mid-call is not trustworthy, and
                # `_evict` now closes it in the task that opened it.
                await _evict(key)
                raise

    @asynccontextmanager
    async def connect(self):
        """
        Public async context manager yielding an initialised `ClientSession`.
        Resolves server config and credentials exactly once, then delegates to
        the pool via `_session`.
        """
        server = await self.get_server_config()
        resolved = await self._resolve_credentials(server)
        async with self._session(server, resolved) as session:
            yield session

    @asynccontextmanager
    async def _connect_stdio(self, server: MCPServer, resolved: ResolvedCredentials):
        command = server.command
        if not command:
            raise ValueError(f"MCP server '{server.name}' is stdio but has no command")

        if not os.path.isabs(command):
            command = shutil.which(command) or command

        merged_env = {**os.environ, **(server.env or {}), **resolved.env_vars}
        params = StdioServerParameters(command=command, args=server.args or [], env=merged_env)

        # The subprocess's stderr is where the useful diagnosis lives — npm's
        # "404 Not Found", a missing runtime, a rejected token. By default the
        # SDK forwards it to our own stderr and the exception carries none of
        # it, which is why a nonexistent package surfaced as nothing more than
        # "unhandled errors in a TaskGroup".
        errlog = _StderrTap(server.name)
        try:
            async with stdio_client(params, errlog=errlog) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    yield session
        except Exception as e:
            detail = errlog.summary()
            logger.warning(
                "Failed stdio connection to MCP server %s: %s%s",
                server.name, _describe(e), f" — {detail}" if detail else "",
                exc_info=True,
            )
            raise MCPConnectionError(
                f"{_describe(e)}{f' — {detail}' if detail else ''}"
            ) from e
        finally:
            errlog.close()

    @asynccontextmanager
    async def _connect_sse(self, server: MCPServer, resolved: ResolvedCredentials):
        if not server.url:
            raise ValueError(f"MCP server '{server.name}' is SSE but has no URL")

        # Re-validate at connection time, not just at registration: DNS for a
        # hostname that passed validation once can later resolve to a private
        # address (DNS rebinding), so the guard has to run against the URL we
        # are about to actually connect to.
        from core.safety.net import assert_url_safe
        await asyncio.to_thread(assert_url_safe, server.url)

        kwargs: dict[str, Any] = {}
        if resolved.headers:
            # Newer versions of the mcp SDK accept a `headers=` kwarg; fall back
            # silently if this version doesn't, rather than crashing.
            sig = inspect.signature(sse_client)
            if "headers" in sig.parameters:
                kwargs["headers"] = resolved.headers
            else:
                logger.warning(
                    "sse_client in this mcp version does not accept headers; "
                    "auth headers for server %s will not be sent.",
                    server.name,
                )

        try:
            async with sse_client(server.url, **kwargs) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    yield session
        except Exception as e:
            logger.exception("Failed SSE connection to MCP server %s", server.name)
            raise MCPConnectionError(_describe(e)) from e

    async def list_tools(self, use_cache: bool = True) -> list[dict[str, Any]]:
        """
        Return tool descriptors for this server.

        Cached (Redis) by default with a short TTL; pass `use_cache=False`
        to force a live fetch (used by the tool-cache invalidation path
        and by debug endpoints).

        Server config and credentials are resolved exactly once regardless of
        whether the cache is warm or cold.

        Listing is discovery, so it does not require the connection to be
        enabled — see `get_server_config`. Callers that iterate servers already
        filter on enablement themselves (`get_servers_for_user`), so this only
        widens the single-server case the Connections page asks about.
        """
        server = await self.get_server_config(require_enabled=False)
        user_id = _coerce_user_id(self.user)

        if use_cache:
            cached = await MCPToolCache.get(self.server_id, user_id)
            if cached is not None:
                return cached

        resolved = await self._resolve_credentials(server)
        async with self._session(server, resolved) as session:
            # Bounded separately from the connect: a server can complete the
            # handshake and then never answer, and an unbounded await here
            # would hold the pool entry's lock for the life of the process.
            result = await asyncio.wait_for(session.list_tools(), timeout=RPC_TIMEOUT)
            tools = [
                {
                    "name": t.name,
                    "description": t.description or "",
                    "inputSchema": t.inputSchema or {"type": "object", "properties": {}},
                }
                for t in result.tools
            ]

        await MCPToolCache.set(self.server_id, user_id, tools)
        return tools

    async def call_tool(self, tool_name: str, arguments: dict[str, Any] | None = None) -> Any:
        """Execute a single tool and return a JSON-friendly payload."""
        server = await self.get_server_config()
        resolved = await self._resolve_credentials(server)
        async with self._session(server, resolved) as session:
            result: CallToolResult = await asyncio.wait_for(
                session.call_tool(tool_name, arguments or {}), timeout=CALL_TOOL_TIMEOUT
            )

        if result.isError:
            raise RuntimeError(f"MCP tool '{tool_name}' reported error: {result}")

        return _serialise_tool_result(result)



def _serialise_tool_result(result: CallToolResult) -> Any:
    """Translate MCP CallToolResult content blocks into JSON-safe Python."""
    parts: list[Any] = []
    for content in result.content:
        ctype = getattr(content, "type", None)
        if ctype == "text":
            parts.append(content.text)
        elif ctype == "image":
            parts.append({
                "type": "image",
                "mime_type": getattr(content, "mimeType", None),
                "data": getattr(content, "data", None),
            })
        elif ctype == "resource":
            res = getattr(content, "resource", content)
            parts.append({
                "type": "resource",
                "uri": getattr(res, "uri", None),
                "mime_type": getattr(res, "mimeType", None),
                "text": getattr(res, "text", None),
            })
        else:
            parts.append(str(content))

    if len(parts) == 1:
        return parts[0]
    return parts


async def drain_pool() -> None:
    """
    Close all pooled sessions and clear the pool.

    Call this on process shutdown (e.g. Django AppConfig.ready teardown or
    a test fixture) to cleanly terminate stdio subprocesses and SSE streams.
    """
    keys = list(_pool.keys())
    for key in keys:
        await _evict(key)
    _creation_locks.clear()
    _failures.clear()


def _visible_servers_queryset(user_id: int | None, enabled_only: bool = True):
    qs = MCPServer.objects.all()
    if enabled_only:
        qs = qs.filter(enabled=True)
        if user_id is not None:
            # A user's explicit "off" wins over the server's own default. Without
            # this the Connections toggle would be cosmetic: the server would
            # keep advertising its tools on every agent turn.
            qs = qs.exclude(
                id__in=MCPServerPreference.objects.filter(
                    user_id=user_id, enabled=False
                ).values('server_id')
            )
    if user_id is None:
        return qs.filter(user__isnull=True)
    return qs.filter(Q(user__isnull=True) | Q(user_id=user_id))


def _get_visible_server_sync(server_id: int, user_id: int | None, enabled_only: bool = True) -> MCPServer | None:
    return _visible_servers_queryset(user_id, enabled_only).filter(id=server_id).first()


def _servers_for_user_sync(user_id: int | None):
    """Servers visible to this user (their own + system-wide)."""
    return list(_visible_servers_queryset(user_id, enabled_only=True))


async def get_servers_for_user(user) -> list[MCPServer]:
    """Return enabled MCPServer rows visible to the given user or user_id."""
    return await sync_to_async(_servers_for_user_sync)(_coerce_user_id(user))


async def get_all_tools_from_all_servers(user) -> list[dict[str, Any]]:
    """Aggregate tools from every server visible to `user`, with origin tags."""
    servers = await get_servers_for_user(user)
    tools: list[dict[str, Any]] = []

    async def collect_server_tools(server: MCPServer) -> list[dict[str, Any]]:
        try:
            manager = MCPClientManager(server.id, user=user)
            server_tools = await asyncio.wait_for(manager.list_tools(), timeout=LIST_TOOLS_TIMEOUT)
            return [
                {
                    **t,
                    "server_id": server.id,
                    "server_name": server.name,
                }
                for t in server_tools
            ]
        except asyncio.TimeoutError:
            logger.warning("Timed out listing tools for MCP server %s", server.name)
        except Exception as e:  # noqa: BLE001 — one bad connector must not
            # empty the whole toolbox, so every failure degrades to "no tools
            # from this server" and the others are still gathered.
            logger.warning("Could not list tools for MCP server %s: %s", server.name, _describe(e))
        return []

    results = await asyncio.gather(
        *(collect_server_tools(server) for server in servers),
        return_exceptions=True,
    )
    for server_tools in results:
        if isinstance(server_tools, BaseException):
            logger.warning("MCP tool collection failed: %s", _describe(server_tools))
            continue
        tools.extend(server_tools)
    return tools
