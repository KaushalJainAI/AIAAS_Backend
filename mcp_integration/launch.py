"""
How a stdio connector is actually started.

A catalogue row says `npx -y @modelcontextprotocol/server-memory`, and that
spelling is deliberate: it is the one command that works on a developer's
machine with nothing installed. In the container it is also the most expensive
way to start anything, because `npx` is itself a Node process that resolves the
package, spawns a *second* Node for the server, and then stays resident for the
life of the connector. Two interpreters per connector, of which one does
nothing but hold a pipe.

So the row keeps saying `npx -y <pkg>` and this module rewrites it, at spawn
time, to `node /opt/mcp/node_modules/<pkg>/<bin>` whenever that package is
present in the image. One process instead of two, and no resolution step —
which is also most of the cold-start latency the pre-warmed npx cache existed to
hide.

Three rules:

* **Rewrite only what is provably the same program.** The package must be
  installed at `PACKAGE_ROOT` and its `package.json` must name a bin script
  that exists on disk. Anything else — a `uvx` row, a bare command, a package
  that is not in the image — is returned untouched and starts exactly as before.
* **Never guess the bin.** `package.json`'s `bin` is a string or a map; when it
  is a map with several entries the one matching the package's own short name
  wins, and a map with no such entry is left to `npx`, because picking the first
  key would silently start a different program.
* **Results are cached per package spec**, since this runs on the connect path
  and the answer changes only when the image does.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import threading

logger = logging.getLogger(__name__)

#: Where the image installs the curated connector packages. Empty disables the
#: rewrite entirely, which is what a developer without the image gets.
PACKAGE_ROOT: str = os.environ.get("MCP_PACKAGE_ROOT", "/opt/mcp/node_modules")

_cache: dict[str, tuple[str, list[str]] | None] = {}
_cache_lock = threading.Lock()


def _package_spec(command: str, args: list[str]) -> str | None:
    """The npm package an `npx` invocation would run, or None if not an npx row."""
    if os.path.basename(command).split(".")[0] != "npx":
        return None
    for arg in args:
        if arg.startswith("-"):  # -y, --yes, --package=... etc.
            continue
        # A version pin (`pkg@1.2.3`) names a specific build; the image holds
        # whatever `npm install` resolved, which is not necessarily it.
        if "@" in arg[1:]:
            return None
        return arg
    return None


def _bin_path(package: str) -> str | None:
    """The executable script for an installed package, or None."""
    pkg_dir = os.path.join(PACKAGE_ROOT, *package.split("/"))
    manifest = os.path.join(pkg_dir, "package.json")
    try:
        with open(manifest, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None

    bin_field = data.get("bin")
    rel: str | None = None
    if isinstance(bin_field, str):
        rel = bin_field
    elif isinstance(bin_field, dict):
        short = package.rsplit("/", 1)[-1]
        candidate = bin_field.get(short)
        if candidate is None and len(bin_field) == 1:
            candidate = next(iter(bin_field.values()))
        rel = candidate if isinstance(candidate, str) else None
    if not rel:
        return None

    path = os.path.normpath(os.path.join(pkg_dir, rel))
    # Containment check: a malicious or merely odd `bin` of "../../x" must not
    # turn a catalogue row into a path anywhere on the filesystem.
    if not path.startswith(os.path.join(PACKAGE_ROOT, "")):
        return None
    return path if os.path.isfile(path) else None


def resolve_launch(command: str, args: list[str]) -> tuple[str, list[str]]:
    """Rewrite an `npx` launch to a direct `node` launch where possible.

    Returns `(command, args)` — the original pair when no rewrite applies, so
    every caller can use the result unconditionally.
    """
    if not PACKAGE_ROOT:
        return command, args
    package = _package_spec(command, args)
    if package is None:
        return command, args

    with _cache_lock:
        if package in _cache:
            cached = _cache[package]
            return cached if cached is not None else (command, args)

    resolved: tuple[str, list[str]] | None = None
    script = _bin_path(package)
    if script:
        node = shutil.which("node")
        if node:
            # Everything after the package name is the server's own argv (the
            # filesystem connector takes its allowed directories this way).
            tail = args[args.index(package) + 1:] if package in args else []
            resolved = (node, [script, *tail])
            logger.info("Launching MCP package %s directly via node", package)

    with _cache_lock:
        _cache[package] = resolved
    return resolved if resolved is not None else (command, args)


def clear_cache() -> None:
    """Tests only — the resolution is otherwise fixed for the life of the image."""
    with _cache_lock:
        _cache.clear()
