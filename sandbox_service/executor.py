"""Spawn one locked-down subprocess per snippet and collect its envelope.

This is the parent side of `runner.py`. It is importable and testable on its
own (the tests exercise it directly), and it is what `server.py` calls per
request.

What confines a run, from outermost to innermost:

1. The *container* (Dockerfile + compose): no network egress, `cap_drop: ALL`,
   read-only root, non-root user, memory + pids caps. This is the real boundary
   — a full breakout of the interpreter still lands in a throwaway container
   with no secrets and nowhere to go.
2. This module: a fresh process in its own session (so a fork bomb dies with
   `killpg`), `setrlimit` for CPU/address-space/file-size/core, a scrubbed
   environment, and an ephemeral cwd removed afterwards.
3. `runner.py`: a best-effort seccomp filter blocking socket creation.

`setrlimit`, `setsid` and `killpg` are POSIX-only; on Windows (local dev, where
the service does not run) they degrade to a plain timeout-and-kill so the tests
still exercise the envelope plumbing.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile

_POSIX = os.name == "posix"

if _POSIX:
    import resource  # noqa: E402  (POSIX-only)

RUNNER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "runner.py")

# The env a snippet is allowed to see. Nothing from the service's own
# environment leaks in — not that the container holds secrets, but a scrubbed
# env is one less thing to reason about. Threads pinned to 1 so a BLAS call in
# numpy cannot fan out across every core on a shared box.
_BASE_ENV = {
    "PATH": "/usr/local/bin:/usr/bin:/bin",
    "PYTHONHASHSEED": "0",
    "OMP_NUM_THREADS": "1",
    "OPENBLAS_NUM_THREADS": "1",
    "MKL_NUM_THREADS": "1",
    "NUMEXPR_NUM_THREADS": "1",
    "HOME": "/tmp",
    "TMPDIR": "/tmp",
    "LANG": "C.UTF-8",
}

_TIMEOUT_ENVELOPE = {
    "success": False,
    "result": None,
    "output": "",
    "stderr": "",
    "error": None,
    "timed_out": True,
}


def _limits(cpu_seconds: int, mem_bytes: int, fsize_bytes: int):
    """Return a preexec_fn applying rlimits, or None off POSIX."""
    if not _POSIX:
        return None

    def _set():
        os.setsid()  # own process group, so killpg reaps children/threads
        # Hard CPU ceiling: even if the wall-clock kill is missed, the kernel
        # sends SIGXCPU. Soft one second under hard so a snippet can catch it.
        resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds + 1))
        resource.setrlimit(resource.RLIMIT_AS, (mem_bytes, mem_bytes))
        resource.setrlimit(resource.RLIMIT_FSIZE, (fsize_bytes, fsize_bytes))
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))

    return _set


def execute(
    code: str,
    *,
    wall_seconds: int = 10,
    cpu_seconds: int = 8,
    mem_bytes: int = 512 * 1024 * 1024,
    fsize_bytes: int = 16 * 1024 * 1024,
    max_output: int = 200_000,
) -> dict:
    """Run `code` in a subprocess and return the normalized envelope.

    The contract mirrors the in-process engine so `sandbox.engine` can treat
    both the same: keys `success`, `result`, `output`, `stderr`, `error`,
    `timed_out`.
    """
    workdir = tempfile.mkdtemp(prefix="sbx-")
    try:
        proc = subprocess.Popen(
            [sys.executable, "-I", "-B", RUNNER],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=workdir,
            env=_BASE_ENV,
            preexec_fn=_limits(cpu_seconds, mem_bytes, fsize_bytes),
            start_new_session=not _POSIX,  # POSIX uses setsid in preexec instead
        )
        try:
            stdout, stderr = proc.communicate(input=code.encode("utf-8"), timeout=wall_seconds)
        except subprocess.TimeoutExpired:
            _kill(proc)
            stdout, stderr = proc.communicate()
            env = dict(_TIMEOUT_ENVELOPE)
            env["error"] = f"Execution timed out after {wall_seconds}s and was killed."
            env["stderr"] = (stderr or b"").decode("utf-8", "replace")[:max_output]
            return env

        return _parse(stdout, stderr, proc.returncode, max_output)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def _kill(proc: subprocess.Popen) -> None:
    import signal
    try:
        if _POSIX:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        else:
            proc.kill()
    except (ProcessLookupError, PermissionError, OSError):
        pass


def _parse(stdout: bytes, stderr: bytes, returncode: int, max_output: int) -> dict:
    text = (stdout or b"").decode("utf-8", "replace").strip()
    err_text = (stderr or b"").decode("utf-8", "replace")

    # Normal path: runner.py wrote one JSON envelope to stdout.
    if text:
        try:
            env = json.loads(text)
            if isinstance(env, dict) and "success" in env:
                env.setdefault("timed_out", False)
                env["output"] = (env.get("output") or "")[:max_output]
                env["stderr"] = (env.get("stderr") or "")[:max_output]
                return env
        except (ValueError, TypeError):
            pass

    # The child died before it could emit an envelope (OOM-killed, SIGXCPU,
    # seccomp trap, segfault in a C extension). Report it as a failure with
    # whatever the kernel/interpreter left on stderr.
    detail = err_text.strip() or f"Process exited with code {returncode} before returning a result."
    return {
        "success": False,
        "result": None,
        "output": "",
        "stderr": err_text[:max_output],
        "error": detail[:max_output],
        "timed_out": False,
    }
