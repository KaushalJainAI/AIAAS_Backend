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
import threading
import time
from collections import OrderedDict
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from typing import Any

import asyncio

from asgiref.sync import sync_to_async
from django.conf import settings
from django.core.exceptions import PermissionDenied
from django.db.models import Q
from mcp import ClientSession, StdioServerParameters
from mcp.client.sse import sse_client
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamablehttp_client
from mcp.types import CallToolResult

from workflow_backend.background import spawn

from .credential_injector import CredentialInjector, ResolvedCredentials, _coerce_user_id
from .launch import resolve_launch
from .models import MCPServer, MCPServerPreference
from .supervisor import (
    ConnectorBudgetExceeded,
    descendants,
    kill_tree,
    proc_available,
    supervisor,
)
from .tool_cache import MCPToolCache

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Connection pool
# ---------------------------------------------------------------------------

def _float_env(name: str, default: float) -> float:
    """A timeout knob overridable without a rebuild. A bad value keeps the
    default rather than refusing to boot the process over one knob."""
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


#: Seconds a session stays alive without activity.
#:
#: Env-driven since 2026-09-17: on a RAM-tight box an idle connector holding a
#: Node process for five minutes is five minutes of budget nobody is using, and
#: the restart it saves costs ~1-2 s once packages are launched directly (see
#: `launch.py`). Production sets 120; the default is unchanged so no other
#: environment quietly changes behaviour.
SESSION_TTL: float = _float_env("MCP_SESSION_TTL", 300.0)
#: How many live sessions the pool may hold at once.
#:
#: The pool had a TTL and no size cap, so the number of live stdio subprocesses
#: was bounded by user behaviour rather than by configuration: N users x M
#: connectors all stay resident until each happens to idle out. Every stdio
#: entry is a Node process on a RAM-tight box, so that is the shape of an OOM,
#: not of a cache.
#:
#: The cap and the TTL answer different questions and both are kept. The cap is
#: "how many may live at once"; the TTL is "how long may a session nobody is
#: using hold a subprocess". A cap alone would keep the N most recent sessions
#: resident for ever on a quiet box; a TTL alone is what we had.
#:
#: Six because the box holds ~1.9 GB total beside Redis, the sandbox sidecar and
#: Django, and a connector's Node process is tens of megabytes. Raise it on a
#: larger host — this is a memory ceiling, not a correctness bound.
MAX_POOLED_SESSIONS: int = int(_float_env("MCP_MAX_POOLED_SESSIONS", 6))
# `npx -y <pkg>` resolves and installs before the server prints a byte: measured
# 8.5 s for a working connector on this catalogue and 7.7 s for npm to report
# E404 on a missing one. The old 5 s budget was shorter than either, so *every*
# stdio connector timed out — a working one and a nonexistent package were
# indistinguishable, both surfacing as a bare `TimeoutError` with no message.
CONNECT_TIMEOUT: float = 25.0
LIST_TOOLS_TIMEOUT: float = 30.0
# Short on purpose: close runs on the request's hot path (eviction during a
# turn), and a wedged transport that needs more than a few seconds is never
# going to close cleanly — the cancellation below still runs in the worker's
# own task. 10s here turned every listing timeout into a second timeout.
CLOSE_TIMEOUT: float = _float_env("MCP_CLOSE_TIMEOUT", 5.0)
# How long a connection failure is remembered. Without this every open of a
# broken connector's card spawns another npx that takes ~8 s to fail; a user
# clicking around a catalogue of eleven can have a dozen in flight.
FAILURE_TTL: float = 60.0
# A memory refusal is a different claim from a broken connector: it says "not
# now", and what it waits on (an idle session timing out, a turn finishing) is
# measured in seconds. Remembering it for a full minute would take a working
# connector away long after the room came back.
BUDGET_FAILURE_TTL: float = _float_env("MCP_BUDGET_FAILURE_TTL", 10.0)
# Budgets for RPCs on an already-open session. Separate from the connect budget
# because a server that handshook and then went mute is a different failure from
# one that never started.
RPC_TIMEOUT: float = 15.0
CALL_TOOL_TIMEOUT: float = 120.0
# Retired 2026-09-17: what an agent turn would wait for a *cold* connector
# before going without it. There is no such wait any more — a listing is
# answered from storage or not at all (`list_tools(cached_only=True)`), so the
# turn spawns nothing and waits for nothing. The budget it named could never be
# met by the thing it was bounding: a cold start is ~21s against a 5s ceiling,
# so every cold connector bought 5s of silence and returned no tools anyway.
# `MCP_AGENT_LIST_TIMEOUT` is therefore read by nothing; it is left documented
# here rather than as a constant, because a knob that moves nothing is the same
# lie as a switch that writes an unread row.

# ---------------------------------------------------------------------------
# Subprocess environment
# ---------------------------------------------------------------------------

# A stdio MCP server is a third-party program we spawn. It used to inherit the
# whole of `os.environ`, which in a deployed container is `Backend/.env` — so
# every curated npm package received `SECRET_KEY`, `POSTGRES_PASSWORD`, and
# above all `CREDENTIAL_ENCRYPTION_KEY`, the master key for *every* user's
# vault. That silently undid the entire point of the credentials app: resolve
# per-user, inject only the mapped fields, never hand out the key.
#
# So the environment is now built rather than inherited. Only what a launcher
# genuinely needs is passed through; everything else must come from
# `server.env` (non-secret, operator-set) or `resolved.env_vars` (the
# injector's per-user mapping). Adding a name here widens what third-party
# code can read, so add one only with a reason.
_ENV_PASSTHROUGH: frozenset[str] = frozenset({
    # Process/exec basics.
    "PATH", "PATHEXT", "COMSPEC", "SYSTEMROOT", "WINDIR", "SYSTEMDRIVE",
    "TEMP", "TMP", "TMPDIR",
    # Where npm/npx look for their cache and config.
    "HOME", "USERPROFILE", "HOMEDRIVE", "HOMEPATH",
    "APPDATA", "LOCALAPPDATA", "XDG_CACHE_HOME", "XDG_CONFIG_HOME",
    # Locale, so a server's own output is not mojibake.
    "LANG", "LC_ALL", "LC_CTYPE",
    # Egress through a corporate proxy, and the CA bundle that makes it work.
    "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY",
    "SSL_CERT_FILE", "SSL_CERT_DIR", "NODE_EXTRA_CA_CERTS",
})

# Prefixes passed through wholesale: node/npm read a long tail of these and
# enumerating them would be a maintenance burden with no security gain — they
# are the launcher's own configuration, not ours.
_ENV_PASSTHROUGH_PREFIXES: tuple[str, ...] = ("NODE_", "NPM_", "NPM_CONFIG_", "UV_", "PYTHONIO")


def _build_subprocess_env(server: "MCPServer", resolved: "ResolvedCredentials") -> dict[str, str]:
    """
    The environment a stdio MCP server is started with.

    Precedence is passthrough < `server.env` < injected credentials, so an
    operator can override a launcher variable and the injector always wins over
    both. Windows reports env names in mixed case, so matching is done on the
    upper-cased name while the original spelling is preserved in the result.
    """
    env: dict[str, str] = {}
    for key, value in os.environ.items():
        upper = key.upper()
        if upper in _ENV_PASSTHROUGH or upper.startswith(_ENV_PASSTHROUGH_PREFIXES):
            env[key] = value
    env.update(server.env or {})
    env.update(resolved.env_vars)
    return env


def _write_credential_files(
    server: "MCPServer", user_id: int | None, resolved: "ResolvedCredentials"
) -> tuple[str | None, dict[str, str]]:
    """
    Write a server's rendered credential files into a private directory.

    Returns `(directory, extra_env)` — the directory so the caller can remove
    it, and the env vars pointing at what was written. `(None, {})` when the
    server asked for no files, which is every connector but the Google ones.

    Two properties this has to hold, both of them about a plaintext refresh
    token sitting on a disk:

    * **One directory per (server, user).** `mkdtemp` gives a fresh path with
      0700 permissions every call, so two users of the same curated row never
      share one — the same isolation the session pool key already provides, and
      the reason the caller passes `user_id` even though it does not appear in
      the path. The pool holds one worker per key, so one live directory per
      key follows.
    * **Removed by whoever created it.** The caller is `_SessionWorker`, which
      opens and unwinds in a single task; cleanup rides that same unwind. A
      directory removed on a different task is the anyio bug this class was
      written to avoid, and here it would leave the token behind.
    """
    if not resolved.files:
        return None, {}

    directory = tempfile.mkdtemp(prefix=f"mcpcred-{server.id}-")
    extra_env: dict[str, str] = {}
    try:
        for spec in resolved.files:
            path = os.path.join(directory, spec.filename)
            # Opened 0600 *before* anything is written, so the secret is never
            # briefly world-readable between create and chmod.
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(spec.content)
            extra_env[spec.env_var] = directory if spec.target == "dir" else path
    except BaseException:
        _remove_credential_dir(directory)
        raise
    return directory, extra_env


def _remove_credential_dir(directory: str | None) -> None:
    """Best-effort removal. Failing to clean up must not fail a run that has
    already produced its answer — but it is logged, because a leftover
    directory holds a usable refresh token."""
    if not directory:
        return
    try:
        shutil.rmtree(directory)
    except FileNotFoundError:
        # Already gone. Cleanup runs on every unwind including ones where the
        # connect failed before anything was written, so this is the ordinary
        # case and not worth a warning.
        pass
    except Exception:  # noqa: BLE001
        logger.warning("Could not remove MCP credential dir %s", directory, exc_info=True)


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

    def __init__(
        self,
        manager: "MCPClientManager",
        server: "MCPServer",
        resolved: "ResolvedCredentials",
        key: "_PoolKey | None" = None,
    ):
        self._manager = manager
        self._server = server
        self._resolved = resolved
        self._key = key
        self._ready = asyncio.Event()
        self._closing = asyncio.Event()
        self.session: ClientSession | None = None
        self.error: BaseException | None = None
        self.task: asyncio.Task | None = None
        self._cred_dir: str | None = None
        #: Subprocesses this session started, so eviction can make sure they
        #: are gone rather than trusting the transport's own unwind. Captured
        #: as the difference in our descendants across the spawn, which is
        #: unambiguous because `supervisor.MAX_CONCURRENT_STARTS` serialises
        #: starts — that is the second reason it exists, after the memory spike.
        self.pids: set[int] = set()

    async def _run(self) -> None:
        """
        The worker body. It never raises: a failure is recorded on `self.error`
        for the opener to read and the task ends normally, so a broken
        connector cannot leave an unretrieved task exception behind on a loop
        that has to keep serving every other request.
        """
        try:
            # Credential files are created here, in the task that will remove
            # them, and before the transport opens — a server that reads its
            # credentials at startup must find them already on disk.
            self._cred_dir, file_env = await asyncio.to_thread(
                _write_credential_files,
                self._server, _coerce_user_id(self._manager.user), self._resolved,
            )
            if file_env:
                self._resolved = replace(
                    self._resolved,
                    env_vars={**self._resolved.env_vars, **file_env},
                )
            async with contextlib.AsyncExitStack() as stack:
                if self._server.type == "stdio":
                    cm = self._manager._connect_stdio(self._server, self._resolved)
                elif self._server.type == "http":
                    cm = self._manager._connect_http(self._server, self._resolved)
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
            # The subprocess is gone by now, so the tokens on disk have no
            # further reader. Removed unconditionally — a failed connect leaves
            # a directory just as readable as a successful one.
            _remove_credential_dir(self._cred_dir)
            self._cred_dir = None
            # Whether we connected, failed, or were cancelled, the opener must
            # stop waiting or it hangs for the full connect budget.
            self._ready.set()

    async def start(self) -> None:
        """Spawn the worker and wait for the session to become usable."""
        before = descendants() if proc_available() else set()
        # Detached: this task outlives the request that opened it, so it must
        # not inherit that request's asgiref executor (see background.spawn).
        self.task = spawn(self._run(), name=f"mcp-session-{self._server.id}")
        try:
            await asyncio.wait_for(self._ready.wait(), timeout=CONNECT_TIMEOUT)
        except asyncio.TimeoutError:
            self._capture_pids(before)
            await self.close()
            raise MCPConnectionError(
                f"Timed out after {CONNECT_TIMEOUT:.0f}s connecting to "
                f"'{self._server.name}'."
            ) from None
        self._capture_pids(before)
        if self.session is None:
            await self.close()
            raise MCPConnectionError(
                f"Could not connect to '{self._server.name}': {_describe(self.error)}"
            )

    def _capture_pids(self, before: set[int]) -> None:
        """Record the subprocesses this spawn added, if any."""
        if not proc_available():
            return
        try:
            self.pids |= descendants() - before
        except Exception:  # noqa: BLE001 — accounting must never fail a connect
            logger.debug("Could not capture MCP subprocess pids", exc_info=True)

    async def close(self) -> None:
        """Ask the worker to unwind, and wait briefly for it to finish.

        Whatever happens to the transport, the accounting is released and any
        process this session started is made sure of. A cancel that leaves a
        Node process resident is how the budget drifts away from reality — and
        `did not close in time` says that is not a hypothetical.
        """
        self._closing.set()
        task = self.task
        try:
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
        finally:
            if self._key is not None:
                supervisor.release(self._key)


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
#
# An `OrderedDict` rather than a plain one because the order *is* the eviction
# policy: `_touch` moves a key to the end when its session is actually borrowed,
# so iterating front-to-back yields least-recently-used first. Insertion order
# alone would evict by age, which would throw out the connector being used on
# every turn and keep one touched once an hour ago.
_pool: "OrderedDict[_PoolKey, _PoolEntry]" = OrderedDict()
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


def _record_failure(key: _PoolKey, message: str, ttl: float = FAILURE_TTL) -> None:
    _failures[key] = (time.monotonic() + ttl, message)


async def _evict(key: _PoolKey) -> None:
    """Close and remove a pool entry; safe to call when it doesn't exist."""
    entry = _pool.pop(key, None)
    if entry is not None:
        try:
            await entry.worker.close()
        except Exception:  # noqa: BLE001 — eviction must never raise
            logger.debug("Error closing MCP session for %s", key, exc_info=True)


def _touch(key: _PoolKey) -> None:
    """Mark a session as most recently used.

    Called where the session is actually borrowed rather than where the entry
    is looked up, because "recently used" has to mean used. Keying recency off
    creation instead would make the cap evict by age and throw out the
    connector every turn is calling.
    """
    if key in _pool:
        _pool.move_to_end(key)


async def _close_evicted(
    key: _PoolKey, entry: "_PoolEntry", pids: set[int] | None = None,
) -> None:
    try:
        await entry.worker.close()
    except Exception:  # noqa: BLE001 — eviction must never raise
        logger.debug("Error closing evicted MCP session for %s", key, exc_info=True)
    finally:
        # The accounting was already freed by the eviction; this is the reap.
        # A transport that unwound cleanly leaves nothing here, and one that had
        # to be cancelled leaves a Node process that the budget has stopped
        # counting — which is the worst of the two states to be in.
        if pids:
            kill_tree(pids)


def _evict_lru_idle(protect: _PoolKey | None = None) -> bool:
    """Drop the least-recently-used session nobody is mid-call on.

    The supervisor calls this when a start does not fit in the memory budget.
    It is the pool's half of admission control and it lives here for the reason
    the supervisor's docstring gives: that module knows about megabytes, this
    one knows which session is least recently used and which is being borrowed
    right now. Returns whether anything was actually freed, because "there is
    nothing left to evict" is what turns a wait into a refusal.

    Prefers a dead or expired entry over a live one — those cost nothing to
    lose — and only then takes the front of the LRU order.
    """
    for key in list(_pool):
        entry = _pool.get(key)
        if entry is None or key == protect or entry.lock.locked():
            continue
        if entry.expired() or not entry.alive():
            _pool.pop(key, None)
            _detach_and_close(key, entry)
            return True

    for key in list(_pool):
        entry = _pool.get(key)
        if entry is None or key == protect or entry.lock.locked():
            continue
        _pool.pop(key, None)
        logger.info("Evicting MCP session %s to free connector memory", key)
        _detach_and_close(key, entry)
        return True

    return False


def _detach_and_close(key: _PoolKey, entry: "_PoolEntry") -> None:
    """Free a session's budget now; close its transport behind the caller.

    The two halves are deliberately split. Accounting has to be released
    *synchronously*, because the caller is usually an admission that is about
    to re-measure and would otherwise refuse a start it has just made room for
    — a close waits up to `CLOSE_TIMEOUT`, which is longer than any admission
    waits. The transport unwind is detached for the reason `_trim_pool` gives:
    it must not sit in front of a live request.
    """
    pids = supervisor.release(key, kill=False)
    spawn(_close_evicted(key, entry, pids), name=f"mcp-evict-{key[0]}")


def _trim_pool(protect: _PoolKey | None = None) -> None:
    """Drop expired sessions, then least-recently-used ones over the cap.

    Synchronous, and the closing is detached, because this runs while a caller
    is waiting to acquire a session: `close()` waits up to `CLOSE_TIMEOUT` for
    a worker to unwind, and putting a dead connector's close budget in front of
    a live turn is the sort of thing this whole change exists to remove. The
    worker unwinds its own exit stack in its own task either way, which is what
    `_SessionWorker` is for, so nothing is orphaned by not awaiting here.

    Expired entries go first and regardless of the cap. The TTL was only ever
    enforced when somebody asked for that same key again, so an idle connector
    on a quiet box held its subprocess indefinitely — the cap alone would not
    have fixed that, and this is what makes "the TTL still means something"
    true rather than merely stated.

    A session someone is mid-call on is never evicted, so the cap is soft by
    design: sitting one over it for the length of a call is a far better
    outcome than breaking the call. `entry.lock` is the same lock `_session`
    holds while a borrowed session is in use.
    """
    for key in list(_pool):
        entry = _pool.get(key)
        if entry is None or key == protect or entry.lock.locked():
            continue
        if entry.expired() or not entry.alive():
            _pool.pop(key, None)
            _detach_and_close(key, entry)

    if MAX_POOLED_SESSIONS <= 0:
        return

    for key in list(_pool):
        if len(_pool) <= MAX_POOLED_SESSIONS:
            return
        entry = _pool.get(key)
        if entry is None or key == protect or entry.lock.locked():
            continue
        _pool.pop(key, None)
        logger.info(
            "Evicting least-recently-used MCP session %s (pool cap %d)",
            key, MAX_POOLED_SESSIONS,
        )
        _detach_and_close(key, entry)


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


#: Refreshes currently in flight, so a burst of turns re-lists a connector once.
#: Without it, five parallel agent workers sharing one stale entry each spawn
#: their own `npx` — the stampede the cache exists to prevent, moved from the
#: foreground to the background where it is harder to notice.
_refreshing: set[tuple[int, int | None]] = set()
#: Guards `_refreshing` across the loop thread and the daemon threads
#: `warm_cache` starts below. Set add/discard is atomic under the GIL, but
#: check-then-add is not — without this, two enables racing each other both
#: see an empty set and both pay a cold `npx`.
_refresh_lock = threading.Lock()


#: Connectors waiting to be re-listed, and the single task draining them.
#:
#: This used to be one detached task per connector, which meant a turn that
#: found eight stale entries started eight connectors at once — the background
#: copy of the same stampede the foreground was careful to avoid. The queue
#: makes re-listing strictly serial: the memory budget would refuse the extras
#: anyway, and a refusal is remembered, so racing them only turns a slow
#: refresh into a failed one.
_refresh_queue: "asyncio.Queue[tuple[int, Any]] | None" = None
_refresh_worker: asyncio.Task | None = None


def _ensure_refresh_worker() -> "asyncio.Queue[tuple[int, Any]]":
    """The refresh queue, with its consumer running on this loop."""
    global _refresh_queue, _refresh_worker

    if _refresh_queue is None or _refresh_worker is None or _refresh_worker.done():
        _refresh_queue = asyncio.Queue()

        async def _drain() -> None:
            assert _refresh_queue is not None
            while True:
                server_id, user = await _refresh_queue.get()
                key = (server_id, _coerce_user_id(user))
                try:
                    await MCPClientManager(server_id, user=user).list_tools(use_cache=False)
                except Exception as e:  # noqa: BLE001
                    # A refresh that fails changes nothing: the stale entry
                    # stays readable until its hard lifetime runs out, which is
                    # strictly better than dropping a working tool list because
                    # one re-list timed out.
                    logger.info(
                        "Background refresh of MCP server %s failed, keeping stale tools: %s",
                        server_id, e,
                    )
                finally:
                    with _refresh_lock:
                        _refreshing.discard(key)
                    _refresh_queue.task_done()

        # Detached: this outlives the request that queued the first refresh, so
        # it must not inherit that request's asgiref executor — every ORM call
        # in the drain would raise once the response had been sent (see
        # `workflow_backend/background.py`).
        _refresh_worker = spawn(_drain(), name="mcp-refresh-queue")
    return _refresh_queue


def _refresh_in_background(server_id: int, user: Any) -> None:
    """Queue a connector re-list, without making anyone wait for it."""
    key = (server_id, _coerce_user_id(user))
    with _refresh_lock:
        if key in _refreshing:
            return
        _refreshing.add(key)

    try:
        _ensure_refresh_worker().put_nowait((server_id, user))
    except Exception:  # noqa: BLE001 — a refresh is best effort by definition
        with _refresh_lock:
            _refreshing.discard(key)
        logger.debug("Could not queue MCP refresh for server %s", server_id, exc_info=True)


def warm_cache(server_id: int, user: Any) -> None:
    """Pre-list a connector's tools after it is configured, without blocking.

    Enabling a connection and then immediately asking the agent something is
    the normal order, and without this the first turn pays the full cold
    `npx` start (~21s) in front of its first token — the one latency the
    stale-serving cache cannot hide, because nothing has been cached yet.

    Safe to call from sync DRF views, which is the whole point: `spawn`
    needs a running loop and there is none in a sync view thread, so this
    runs the refresh on a daemon thread with its own loop instead. The
    thread holds no request state — it re-reads the server row and the
    user's credentials itself — so nothing outlives the response that
    started it. Best-effort throughout: a connector that fails to list
    simply stays cold, exactly as if nothing had been warmed.
    """
    from asgiref.sync import ThreadSensitiveContext
    from django.db import close_old_connections

    key = (server_id, _coerce_user_id(user))
    with _refresh_lock:
        if key in _refreshing:
            return
        _refreshing.add(key)

    async def _warm() -> None:
        # One thread for all of the ORM: without the context every
        # `sync_to_async` fans out onto the default executor and each call
        # pins its own connection — the same rule `background._detached`
        # exists to enforce, applied here because there is no `spawn`.
        async with ThreadSensitiveContext():
            try:
                await MCPClientManager(server_id, user=user).list_tools(use_cache=False)
            except Exception as e:  # noqa: BLE001
                logger.info(
                    "Pre-warm of MCP server %s failed, leaving it cold: %s",
                    server_id, e,
                )
            finally:
                with _refresh_lock:
                    _refreshing.discard(key)
                try:
                    await sync_to_async(close_old_connections)()
                except Exception:  # noqa: BLE001
                    logger.exception("[MCP] Failed to close warm-thread connections")

    def _main() -> None:
        try:
            asyncio.run(_warm())
        except Exception:  # noqa: BLE001 — never fail a save over a warm
            logger.exception("[MCP] Warm thread for server %s died", server_id)

    thread = threading.Thread(
        target=_main, name=f'mcp-warm:{server_id}', daemon=True,
    )
    thread.start()


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
        if server.type == "native":
            raise MCPConnectionError(
                f"'{server.name}' is a native connector; its tools are called "
                f"directly, not through an MCP session."
            )
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
                worker = _SessionWorker(self, server, resolved, key=key)
                try:
                    # Admission before spawn, not after: every process this
                    # start is about to create is invisible to the pool cap
                    # until it connects, and eight of them starting at once is
                    # precisely what took the box down. `_evict_lru_idle` is
                    # handed over so the supervisor can make room out of the
                    # cache rather than refusing while an idle connector holds
                    # the megabytes it needs.
                    async with supervisor.admit(
                        key, server.id,
                        evict_lru=lambda: _evict_lru_idle(protect=key),
                    ) as admission:
                        await worker.start()
                        admission.commit(worker.pids)
                except ConnectorBudgetExceeded as exc:
                    # A *budget* refusal is remembered like any other connect
                    # failure so a turn under pressure does not re-queue behind
                    # the same wall on every tool call — but for a fraction of
                    # FAILURE_TTL, because it says "not right now" rather than
                    # "this connector is broken", and the memory it waits on is
                    # freed by an idle timeout measured in seconds.
                    #
                    # A *contention* refusal is not remembered at all: it says
                    # only that another connector happened to be starting, and
                    # holding that against this one would take a healthy
                    # connector away from the next caller for no reason.
                    if not getattr(exc, "transient", False):
                        _record_failure(key, str(exc), ttl=BUDGET_FAILURE_TTL)
                    raise MCPConnectionError(str(exc)) from exc
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
                # Only ever after a successful insert: trimming on the lookup
                # path would let a burst of misses evict live sessions before
                # any of them had been replaced by anything.
                _trim_pool(protect=key)

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
                _touch(key)
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
        if not getattr(settings, "MCP_ALLOW_STDIO", True):
            # Checked at connect time, not only at save, so a stdio row created
            # before the setting was turned off cannot start a process either.
            raise MCPConnectionError(
                f"'{server.name}' runs as a local process, which this deployment "
                f"does not allow. Use a hosted (HTTP) MCP server instead."
            )
        command = server.command
        if not command:
            raise ValueError(f"MCP server '{server.name}' is stdio but has no command")

        args = list(server.args or [])
        # `npx -y <pkg>` is two Node processes: the launcher, which stays
        # resident doing nothing, and the server. Where the image holds the
        # package, start it directly and pay for one.
        command, args = resolve_launch(command, args)

        if not os.path.isabs(command):
            command = shutil.which(command) or command

        merged_env = _build_subprocess_env(server, resolved)
        params = StdioServerParameters(command=command, args=args, env=merged_env)

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

    async def _prepare_remote(
        self, server: MCPServer, resolved: ResolvedCredentials, client_fn,
    ) -> dict[str, Any]:
        """Shared setup for the two URL-based transports: guard, then headers.

        Both `http` and `sse` reach out over the network with the user's
        credentials in a header, so both need the same two things and neither
        should get them by copy-paste.
        """
        if not server.url:
            raise ValueError(
                f"MCP server '{server.name}' is {server.type} but has no URL"
            )

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
            if "headers" in inspect.signature(client_fn).parameters:
                kwargs["headers"] = resolved.headers
            else:
                logger.warning(
                    "%s in this mcp version does not accept headers; auth "
                    "headers for server %s will not be sent.",
                    getattr(client_fn, "__name__", "client"), server.name,
                )
        return kwargs

    @asynccontextmanager
    async def _connect_http(self, server: MCPServer, resolved: ResolvedCredentials):
        """MCP's streamable HTTP transport — what hosted connectors speak.

        Distinct from `_connect_sse` in one way that matters and is easy to get
        wrong by copying it: `streamablehttp_client` yields a **three**-tuple,
        the third element being a callable returning the negotiated session id.
        We do not need it — the pool keys sessions by (server, user) and holds
        the `ClientSession` itself — but unpacking two names from three values
        raises `ValueError` at connect time, which surfaces as an unexplained
        connection failure rather than as the shape mismatch it is.
        """
        kwargs = await self._prepare_remote(server, resolved, streamablehttp_client)

        try:
            async with streamablehttp_client(server.url, **kwargs) as (read, write, _get_session_id):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    yield session
        except Exception as e:
            logger.warning(
                "Failed streamable-HTTP connection to MCP server %s: %s",
                server.name, _describe(e), exc_info=True,
            )
            raise MCPConnectionError(_describe(e)) from e

    @asynccontextmanager
    async def _connect_sse(self, server: MCPServer, resolved: ResolvedCredentials):
        """The deprecated remote transport. Kept for rows pointing at old
        endpoints; new remote servers should be `http`."""
        kwargs = await self._prepare_remote(server, resolved, sse_client)

        try:
            async with sse_client(server.url, **kwargs) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    yield session
        except Exception as e:
            logger.exception("Failed SSE connection to MCP server %s", server.name)
            raise MCPConnectionError(_describe(e)) from e

    async def list_tools(
        self, use_cache: bool = True, cached_only: bool = False,
    ) -> list[dict[str, Any]]:
        """
        Return tool descriptors for this server.

        Cached (Redis, then the stored catalogue) by default; pass
        `use_cache=False` to force a live fetch (used by the tool-cache
        invalidation path and by debug endpoints).

        `cached_only=True` answers from storage or not at all: a miss returns
        `[]` and queues a refresh instead of starting the connector. That is
        what every *listing* caller now passes, and it is the single largest
        thing this class does for latency and for memory. Knowing which tools
        exist is a question about a catalogue; it was answered by spawning a
        Node process, which cost a turn its first seconds and the box its
        headroom — for a list that was, nearly always, already on disk. A
        connector is now started when a tool is *called*.

        Server config and credentials are resolved exactly once regardless of
        whether the cache is warm or cold.

        Listing is discovery, so it does not require the connection to be
        enabled — see `get_server_config`. Callers that iterate servers already
        filter on enablement themselves (`get_servers_for_user`), so this only
        widens the single-server case the Connections page asks about.
        """
        server = await self.get_server_config(require_enabled=False)
        user_id = _coerce_user_id(self.user)

        if server.type == "native":
            # Our own tools, answered from the registry. Nothing to connect to,
            # nothing to cache, and — the reason this row type exists — nothing
            # to spawn. See `mcp_integration/native.py`.
            from .native import tools_for_row

            return tools_for_row(server)

        if use_cache:
            entry = await MCPToolCache.get_entry(self.server_id, user_id)
            if entry is not None:
                tools, stale = entry
                if stale:
                    # Answer from the lapsed copy and re-list behind the
                    # answer. Blocking here is what made a two-minute pause in
                    # a conversation cost an `npx` start before the next reply
                    # began — a cost the user pays and never sees a reason for.
                    _refresh_in_background(self.server_id, self.user)
                return tools

        if cached_only:
            # Nothing stored for this connection yet. Warm it behind the caller
            # — serialised through the refresh queue and subject to the memory
            # budget — and answer without its tools this once.
            _refresh_in_background(self.server_id, self.user)
            logger.info(
                "No stored tool list for MCP server %s; listing in background",
                self.server_id,
            )
            return []

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


def _visible_servers_queryset(user_id: int | None, enabled_only: bool = True):
    # Ordered explicitly, and it matters well beyond tidiness. This list decides
    # the order of the MCP tool descriptors in every model request, and a
    # provider's prompt cache keys on an exact prefix — so an unordered query,
    # which a database is free to answer differently after any write, would
    # reshuffle the tool block between two otherwise identical turns and miss
    # the cache every time. The symptom is pure latency and a larger bill, with
    # nothing anywhere reporting an error. `id` because it is the one column
    # that never changes under a rename.
    qs = MCPServer.objects.order_by('id')
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


def visible_server_ids_sync(user_id: int | None) -> set[int]:
    """The ids of every connection this user could pick, for validating a choice.

    `enabled_only=False` on purpose: this answers "may you name this server",
    not "is it live right now". A user who switches a connection off on the
    Connections page and saves an agent that referenced it should get their
    agent saved, not a validation error about a row they still own — the
    runtime already drops a switched-off server when it resolves the toolbox.
    """
    return set(
        _visible_servers_queryset(user_id, enabled_only=False)
        .values_list('id', flat=True)
    )


def visible_servers_sync(user_id: int | None) -> list[MCPServer]:
    """The rows behind `visible_server_ids_sync`, for offering the choice.

    `enabled_only=False` for exactly the reason that helper gives: this is the
    pool a picker renders, and it has to be the same pool the validator
    accepts. Two predicates would mean a connection the user is shown, picks,
    and is then told does not exist.
    """
    return list(_visible_servers_queryset(user_id, enabled_only=False))


async def get_servers_for_user(user) -> list[MCPServer]:
    """Return enabled MCPServer rows visible to the given user or user_id."""
    return await sync_to_async(_servers_for_user_sync)(_coerce_user_id(user))


async def get_all_tools_from_all_servers(user) -> list[dict[str, Any]]:
    """Aggregate tools from every server visible to `user`, with origin tags."""
    if os.environ.get("MCP_DISABLED", "False").lower() in ("true", "1", "yes"):
        return []
    servers = [s for s in await get_servers_for_user(user) if s.type != "native"]
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
