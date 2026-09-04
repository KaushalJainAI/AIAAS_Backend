"""The Python execution sandbox.

Moved out of `executor/` (2026-08-26), which had been reduced to a husk holding
nothing else. This is a plain Python package, not a Django app: it has no
models, views or urls, so it does not belong in `INSTALLED_APPS`.

There is **one door** — `sandbox.engine`:

    from sandbox.engine import arun_code

It selects between two engines by config (`SANDBOX_ENGINE`):

- ``service`` — the hardened sidecar container in `sandbox_service/`. Real
  kernel-level confinement (no network egress, dropped caps, read-only root,
  non-root, memory/pids caps) plus per-run rlimits and a seccomp filter, and C
  extensions (numpy/pandas) work. This is the production path.
- ``inprocess`` — `safe_execution` (AST denylist + `exec` on a worker thread).
  The local-dev fallback only, and explicitly the weaker of the two; nothing in
  production should select it.

The wasmtime engine and its 40 MB vendored CPython-wasm tree were removed
2026-09-02: it had no callers, and the sidecar is the real isolation the wasm
approach only promised (and could not deliver numpy/pandas).
"""
