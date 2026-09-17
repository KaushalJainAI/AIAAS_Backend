"""
ConnectorSupervisor — the memory ceiling for MCP connector subprocesses.

Why this exists
---------------
A stdio connector is a Node process we spawn, and until this module the only
thing bounding how many could exist was `MAX_POOLED_SESSIONS`, a count over
sessions that had *already connected*. Three gaps in that, all of which the
2026-09-16 production OOM went through at once:

* **A count is not a budget.** Six connectors is 300 MB or 900 MB depending on
  which six, and the box has 913 MB total.
* **Starting is unbounded.** One chat turn listed eight connectors
  concurrently, each start spawning `npx` (one Node) plus the server (another);
  none of them was in the pool yet, so the cap counted none of them.
* **A cap cannot evict a busy session**, so the pool is deliberately allowed to
  sit over its own limit — fine for a count, not fine for the last 100 MB of a
  cgroup.

So the cap stays (it is a good *cache* policy) and this adds the two things it
cannot express: admission control, which asks whether there is room *before*
anything is spawned, and accounting in megabytes actually resident rather than
in sessions.

The shape of it
---------------
`admit()` is the only entry point. It reserves an estimate for the connector
about to start, evicting least-recently-used idle sessions until the estimate
fits, and refuses — quickly, with a message naming the budget — when nothing
can be freed. `commit()` then attributes the processes that appeared to that
key, so the *next* admission reasons about measured megabytes instead of the
default estimate.

Four decisions worth keeping:

* **Measured, not assumed.** `used_mb()` reads RSS for our own descendant
  processes out of `/proc`. An estimate that drifts under-true is how a budget
  becomes decoration, and connectors differ by a factor of three.
* **The cgroup is the backstop.** Staying under the connector budget is not
  enough if Django itself has grown: past `CONTAINER_HIGH_WATER` of the
  container's own limit, admissions evict and then refuse regardless of how
  little the connectors are holding. Something has to lose, and a connector
  losing costs a restart while daphne losing costs the site.
* **Loop-agnostic.** State is guarded by a `threading.Lock`, never an
  `asyncio.Lock`, because `warm_cache` runs this same path on a private loop in
  a daemon thread — an asyncio primitive is bound to the loop that created it
  and would raise there.
* **Degrades to permissive, never to closed.** No `/proc` (Windows dev, a
  restricted container) falls back to counting live sessions against the same
  budget; a budget of 0 disables the ceiling entirely. Losing the ability to
  *measure* must not take away the ability to *run* — the failure this module
  prevents is an OOM, and refusing every connector is its own outage.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

logger = logging.getLogger(__name__)


def _float_env(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _int_env(name: str, default: int) -> int:
    try:
        return int(float(os.environ.get(name, default)))
    except (TypeError, ValueError):
        return default


#: Total resident megabytes every MCP connector subprocess may hold between
#: them. 150 on the production box: the backend container is capped at 384 MB
#: and daphne sits at ~220 MB of that, so this is what is left with headroom.
#: 0 disables the ceiling (local development, or a host with room to spare).
MEMORY_BUDGET_MB: float = _float_env("MCP_MEMORY_BUDGET_MB", 150.0)
#: How many connectors may be *starting* at once. Starting is the expensive
#: moment — `npx` plus the server, before either has trimmed its heap — and it
#: is also the moment nothing else accounts for. 1 on a small box.
MAX_CONCURRENT_STARTS: int = max(1, _int_env("MCP_MAX_CONCURRENT_STARTS", 1))
#: What one connector is assumed to cost until it has been measured once.
#: Deliberately generous: under-estimating admits a connector that then does
#: not fit, which is the exact failure this module exists to prevent.
DEFAULT_CONNECTOR_MB: float = _float_env("MCP_DEFAULT_CONNECTOR_MB", 70.0)
#: How long an admission waits for room before refusing. Short on purpose: the
#: caller is a tool call a user is waiting on, and "no room right now" is a
#: better answer than thirty seconds of silence followed by the same one.
ADMIT_WAIT_SECONDS: float = _float_env("MCP_ADMIT_WAIT_SECONDS", 3.0)
#: Fraction of the *container's* memory limit past which no connector is
#: admitted, whatever the connector budget says.
CONTAINER_HIGH_WATER: float = _float_env("MCP_CONTAINER_HIGH_WATER", 0.85)
#: How long a usage measurement is reused. Scanning `/proc` is cheap but not
#: free and admission polls in a loop.
USAGE_SAMPLE_TTL: float = _float_env("MCP_USAGE_SAMPLE_TTL", 2.0)

_PAGE_SIZE = os.sysconf("SC_PAGE_SIZE") if hasattr(os, "sysconf") else 4096


class ConnectorBudgetExceeded(RuntimeError):
    """No room to start another connector, and nothing idle left to evict."""


# ---------------------------------------------------------------------------
# Process measurement
# ---------------------------------------------------------------------------

def proc_available() -> bool:
    """Whether this host exposes the `/proc` interface we measure through."""
    return os.path.isdir("/proc/self")


def _rss_mb(pid: int) -> float:
    """Resident megabytes for one pid, or 0.0 if it is gone or unreadable."""
    try:
        with open(f"/proc/{pid}/statm", "r", encoding="ascii") as fh:
            fields = fh.read().split()
        # statm: size resident shared text lib data dt — all in pages.
        return int(fields[1]) * _PAGE_SIZE / (1024 * 1024)
    except (OSError, ValueError, IndexError):
        return 0.0


def _parent_map() -> dict[int, int]:
    """pid -> ppid for every visible process.

    A full scan rather than `/proc/<pid>/task/*/children`, which needs
    `CONFIG_PROC_CHILDREN` and is silently absent on some kernels — a
    measurement that reports zero on such a host would quietly disable the
    budget it is supposed to enforce.
    """
    parents: dict[int, int] = {}
    try:
        entries = os.listdir("/proc")
    except OSError:
        return parents
    for entry in entries:
        if not entry.isdigit():
            continue
        pid = int(entry)
        try:
            with open(f"/proc/{pid}/stat", "r", encoding="ascii", errors="replace") as fh:
                data = fh.read()
        except OSError:
            continue
        # The comm field is parenthesised and may itself contain spaces, so the
        # split has to start after the final ')': ppid is the field after state.
        close = data.rfind(")")
        if close == -1:
            continue
        rest = data[close + 2:].split()
        if len(rest) < 2:
            continue
        try:
            parents[pid] = int(rest[1])
        except ValueError:
            continue
    return parents


def descendants(root: int | None = None) -> set[int]:
    """Every process descended from `root` (default: this process)."""
    if not proc_available():
        return set()
    root = os.getpid() if root is None else root
    parents = _parent_map()
    children: dict[int, list[int]] = {}
    for pid, ppid in parents.items():
        children.setdefault(ppid, []).append(pid)
    out: set[int] = set()
    stack = list(children.get(root, ()))
    while stack:
        pid = stack.pop()
        if pid in out:
            continue
        out.add(pid)
        stack.extend(children.get(pid, ()))
    return out


def tree_rss_mb(pids: Iterable[int]) -> float:
    """Resident megabytes held by these pids and everything under them."""
    pids = set(pids)
    if not pids:
        return 0.0
    parents = _parent_map()
    children: dict[int, list[int]] = {}
    for pid, ppid in parents.items():
        children.setdefault(ppid, []).append(pid)
    seen: set[int] = set()
    stack = list(pids)
    total = 0.0
    while stack:
        pid = stack.pop()
        if pid in seen:
            continue
        seen.add(pid)
        total += _rss_mb(pid)
        stack.extend(children.get(pid, ()))
    return total


def kill_tree(pids: Iterable[int]) -> int:
    """SIGKILL these processes and their descendants. Returns how many were signalled.

    Eviction has to actually free the memory it accounted for. Cancelling the
    session task usually takes the subprocess with it, but "usually" is what
    produced `MCP session ... did not close in time` in front of eight fresh
    starts — a wedged transport leaves the Node process resident while the
    budget believes the megabytes came back.
    """
    if not proc_available():
        return 0
    targets: set[int] = set()
    parents = _parent_map()
    children: dict[int, list[int]] = {}
    for pid, ppid in parents.items():
        children.setdefault(ppid, []).append(pid)
    # Only ever signal something still descended from us. Pids are reused, and
    # a recorded pid whose process exited long ago can by then belong to
    # anything on the box — including, on a single-container host, Postgres.
    ours = descendants()
    stack = [pid for pid in pids if pid in ours]
    while stack:
        pid = stack.pop()
        if pid in targets:
            continue
        targets.add(pid)
        stack.extend(children.get(pid, ()))
    killed = 0
    # Deepest first, so a parent cannot respawn or reparent a child between
    # signals.
    for pid in sorted(targets, reverse=True):
        try:
            os.kill(pid, signal.SIGKILL)
            killed += 1
        except (ProcessLookupError, PermissionError):
            continue
        except OSError:  # noqa: PERF203 — one bad pid must not stop the rest
            continue
    return killed


def container_pressure() -> float | None:
    """Used fraction of the container's own memory limit, or None if unlimited.

    cgroup v2 first (what Amazon Linux 2023 and modern Docker use), then v1.
    Returning None rather than 0.0 when there is no limit keeps "unlimited" and
    "empty" distinguishable — the caller ignores the backstop for the former and
    would happily admit on the latter.
    """
    try:
        with open("/sys/fs/cgroup/memory.max", "r", encoding="ascii") as fh:
            raw_max = fh.read().strip()
        if raw_max == "max":
            return None
        limit = float(raw_max)
        with open("/sys/fs/cgroup/memory.current", "r", encoding="ascii") as fh:
            current = float(fh.read().strip())
        return current / limit if limit > 0 else None
    except (OSError, ValueError):
        pass
    try:
        with open("/sys/fs/cgroup/memory/memory.limit_in_bytes", "r", encoding="ascii") as fh:
            limit = float(fh.read().strip())
        with open("/sys/fs/cgroup/memory/memory.usage_in_bytes", "r", encoding="ascii") as fh:
            current = float(fh.read().strip())
        # v1 spells "unlimited" as a number near 2**63.
        if limit <= 0 or limit > 1 << 62:
            return None
        return current / limit
    except (OSError, ValueError):
        return None


# ---------------------------------------------------------------------------
# The supervisor
# ---------------------------------------------------------------------------

@dataclass
class _Live:
    """One connector currently holding processes."""
    server_id: int
    pids: set[int] = field(default_factory=set)
    estimate_mb: float = DEFAULT_CONNECTOR_MB


class ConnectorSupervisor:
    """Admission control and memory accounting for connector subprocesses."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._live: dict[Any, _Live] = {}
        self._reserved: dict[Any, float] = {}
        self._starting = 0
        #: server_id -> measured megabytes, learned on commit. Shared across
        #: users of the same curated connector: what a Node process costs is a
        #: fact about the package, not about whose token it is holding.
        self._costs: dict[int, float] = {}
        self._sample: tuple[float, float] | None = None  # (taken_at, mb)

    # -- accounting ------------------------------------------------------

    def estimate_for(self, server_id: int) -> float:
        with self._lock:
            return self._costs.get(server_id, DEFAULT_CONNECTOR_MB)

    def used_mb(self, *, fresh: bool = False) -> float:
        """Megabytes currently held by connector processes.

        Measured from `/proc` where available. Without it, live sessions are
        counted against their estimates — coarser, but it still bounds the
        number of connectors, which is the property the budget is for.
        """
        now = time.monotonic()
        if not fresh:
            sample = self._sample
            if sample is not None and now - sample[0] < USAGE_SAMPLE_TTL:
                return sample[1]

        with self._lock:
            tracked = {pid for entry in self._live.values() for pid in entry.pids}
            fallback = sum(entry.estimate_mb for entry in self._live.values())

        if proc_available() and tracked:
            mb = tree_rss_mb(tracked)
        elif proc_available() and not self._live:
            mb = 0.0
        else:
            mb = fallback

        self._sample = (now, mb)
        return mb

    def _reserved_mb(self) -> float:
        with self._lock:
            return sum(self._reserved.values())

    def _has_room(self, cost: float) -> tuple[bool, str]:
        """Whether `cost` more megabytes fits, and why not when it does not."""
        if MEMORY_BUDGET_MB <= 0:
            return True, ""
        used = self.used_mb()
        reserved = self._reserved_mb()
        if used + reserved + cost > MEMORY_BUDGET_MB:
            return False, (
                f"connector memory budget reached "
                f"({used + reserved:.0f} MB of {MEMORY_BUDGET_MB:.0f} MB in use, "
                f"{cost:.0f} MB needed)"
            )
        pressure = container_pressure()
        if pressure is not None and pressure > CONTAINER_HIGH_WATER:
            return False, (
                f"container memory at {pressure * 100:.0f}% of its limit "
                f"(ceiling {CONTAINER_HIGH_WATER * 100:.0f}%)"
            )
        return True, ""

    # -- lifecycle -------------------------------------------------------

    def register(self, key: Any, server_id: int, pids: Iterable[int]) -> None:
        """Attribute processes to a connector and learn what it cost.

        Called once a session is up. The measurement is taken here rather than
        sampled later because this is the only moment the new pids are known to
        belong to *this* start: `MAX_CONCURRENT_STARTS` serialises starts
        precisely so the attribution is unambiguous.
        """
        pids = set(pids)
        measured = tree_rss_mb(pids) if (proc_available() and pids) else 0.0
        estimate = self.estimate_for(server_id)
        with self._lock:
            entry = self._live.get(key) or _Live(server_id=server_id, estimate_mb=estimate)
            entry.pids |= pids
            if measured > 0:
                entry.estimate_mb = measured
                # Keep the larger of what we have seen: a connector measured
                # milliseconds after its handshake has not finished allocating,
                # and admitting the next one against that low reading is how a
                # budget overshoots.
                self._costs[server_id] = max(self._costs.get(server_id, 0.0), measured)
            self._live[key] = entry
        self._sample = None

    def release(self, key: Any, *, kill: bool = True) -> set[int]:
        """Forget a connector, killing anything it left behind.

        Returns the pids it was holding, so a caller that passes `kill=False`
        can free the *accounting* now and reap later. That split is what lets
        an eviction admit the next connector immediately: closing a transport
        waits up to `CLOSE_TIMEOUT`, which is longer than an admission is
        willing to wait, so a budget that only freed on close would refuse
        starts it had already made room for.
        """
        with self._lock:
            entry = self._live.pop(key, None)
        if entry is None:
            return set()
        self._sample = None
        if kill and entry.pids:
            killed = kill_tree(entry.pids)
            if killed:
                logger.debug("Reaped %d connector process(es) for %s", killed, key)
        return set(entry.pids)

    def pids_for(self, key: Any) -> set[int]:
        with self._lock:
            entry = self._live.get(key)
            return set(entry.pids) if entry else set()

    def snapshot(self) -> dict[str, Any]:
        """What the budget currently believes. For the debug endpoint and tests."""
        with self._lock:
            live = {
                str(key): {"server_id": e.server_id, "pids": sorted(e.pids),
                           "estimate_mb": round(e.estimate_mb, 1)}
                for key, e in self._live.items()
            }
            starting = self._starting
            reserved = sum(self._reserved.values())
        return {
            "budget_mb": MEMORY_BUDGET_MB,
            "used_mb": round(self.used_mb(fresh=True), 1),
            "reserved_mb": round(reserved, 1),
            "starting": starting,
            "container_pressure": container_pressure(),
            "live": live,
        }

    def reset(self) -> None:
        """Drop all accounting without killing anything. Tests only."""
        with self._lock:
            self._live.clear()
            self._reserved.clear()
            self._costs.clear()
            self._starting = 0
        self._sample = None

    # -- admission -------------------------------------------------------

    async def _acquire_start_slot(self, deadline: float) -> None:
        while True:
            with self._lock:
                if self._starting < MAX_CONCURRENT_STARTS:
                    self._starting += 1
                    return
            if time.monotonic() >= deadline:
                raise ConnectorBudgetExceeded(
                    f"{MAX_CONCURRENT_STARTS} connector start(s) already in "
                    f"progress; try again shortly."
                )
            await asyncio.sleep(0.05)

    def _release_start_slot(self) -> None:
        with self._lock:
            self._starting = max(0, self._starting - 1)

    @contextlib.asynccontextmanager
    async def admit(
        self,
        key: Any,
        server_id: int,
        evict_lru: Callable[[], bool] | None = None,
    ):
        """Reserve room for one connector start, or raise `ConnectorBudgetExceeded`.

        `evict_lru` is called to free a least-recently-used *idle* session; it
        returns whether it evicted anything, and is retried until it says no.
        The caller owns that policy — this module knows about megabytes, the
        pool knows which session is least recently used and which is mid-call.

        Yields a handle whose `commit(pids)` attributes what actually started.
        """
        cost = self.estimate_for(server_id)
        deadline = time.monotonic() + ADMIT_WAIT_SECONDS
        await self._acquire_start_slot(deadline)

        reserved = False
        try:
            while True:
                ok, why = self._has_room(cost)
                if ok:
                    with self._lock:
                        self._reserved[key] = cost
                    reserved = True
                    break
                if evict_lru is not None and evict_lru():
                    # Something was freed. Measure again rather than assuming
                    # the eviction was enough: closing is asynchronous and the
                    # pids may still be unwinding.
                    self._sample = None
                    await asyncio.sleep(0.05)
                    continue
                if time.monotonic() >= deadline:
                    raise ConnectorBudgetExceeded(
                        f"Cannot start connector: {why}. Nothing idle left to evict."
                    )
                await asyncio.sleep(0.1)

            handle = _Admission(self, key, server_id)
            yield handle
        finally:
            if reserved:
                with self._lock:
                    self._reserved.pop(key, None)
                self._sample = None
            self._release_start_slot()


@dataclass
class _Admission:
    supervisor: ConnectorSupervisor
    key: Any
    server_id: int

    def commit(self, pids: Iterable[int]) -> None:
        self.supervisor.register(self.key, self.server_id, pids)


#: Process-wide, like the pool it guards.
supervisor = ConnectorSupervisor()
