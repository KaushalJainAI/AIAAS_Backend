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

**Files.** A run may be handed input files and asked for output files. Both
live in the run's own ephemeral cwd and nowhere else: inputs are written there
before the child starts, and after it exits only the *declared* output names
are read back — regular files only (a symlink the snippet made to somewhere
else is refused, not followed), each under `MAX_FILE_BYTES`, which the child's
own `RLIMIT_FSIZE` already enforces as it writes. Anything else the snippet
created is listed in `unsaved` and deleted with the directory, so a model is
told its extra file was not kept rather than left to believe it was. Names are
bare file names: a separator, `..` or a leading dot is refused before anything
is written.

`setrlimit`, `setsid` and `killpg` are POSIX-only; on Windows (local dev, where
the service does not run) they degrade to a plain timeout-and-kill so the tests
still exercise the envelope plumbing.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import stat
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

#: One input or output file. Matches the child's RLIMIT_FSIZE default below, so
#: a file the snippet could write is a file that can come back.
MAX_FILE_BYTES = 16 * 1024 * 1024
#: Files per run, in each direction.
MAX_FILES = 10
#: A bare file name: no separators, no NUL, not `.`/`..`, no leading dot.
_NAME = re.compile(r'^(?!\.)[^/\\\x00]{1,128}$')


class FileSpecError(ValueError):
    """An input or output name/size the executor refuses before running."""


def check_name(name: str) -> str:
    if not isinstance(name, str) or not _NAME.match(name) or name in ('.', '..'):
        raise FileSpecError(f'{name!r} is not a plain file name.')
    return name


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
    fsize_bytes: int = MAX_FILE_BYTES,
    max_output: int = 200_000,
    files: dict[str, bytes] | None = None,
    collect: tuple[str, ...] | list[str] = (),
) -> dict:
    """Run `code` in a subprocess and return the normalized envelope.

    The contract mirrors the in-process engine so `sandbox.engine` can treat
    both the same: keys `success`, `result`, `output`, `stderr`, `error`,
    `timed_out` — plus `files_out` (`{name: bytes}`), `missing` (declared
    outputs the snippet did not write) and `unsaved` (files it wrote without
    declaring) when `collect` is given.
    """
    files = dict(files or {})
    collect = list(dict.fromkeys(collect or ()))
    if len(files) > MAX_FILES or len(collect) > MAX_FILES:
        raise FileSpecError(f'At most {MAX_FILES} input and {MAX_FILES} output files per run.')
    for name, data in files.items():
        check_name(name)
        if len(data) > MAX_FILE_BYTES:
            raise FileSpecError(f'{name} is over the {MAX_FILE_BYTES // 1_048_576} MB file limit.')
    for name in collect:
        check_name(name)

    workdir = tempfile.mkdtemp(prefix="sbx-")
    try:
        for name, data in files.items():
            with open(os.path.join(workdir, name), 'wb') as handle:
                handle.write(data)

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

        env = _parse(stdout, stderr, proc.returncode, max_output)
        if collect and env.get("success"):
            env.update(_collect(workdir, collect, set(files)))
        return env
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def _collect(workdir: str, collect: list[str], inputs: set[str]) -> dict:
    """Read the declared outputs back; name everything else as unsaved."""
    out: dict[str, bytes] = {}
    missing: list[str] = []
    for name in collect:
        path = os.path.join(workdir, name)
        try:
            info = os.lstat(path)
        except FileNotFoundError:
            missing.append(name)
            continue
        # lstat, not stat: a symlink the snippet planted is refused rather
        # than followed to whatever it points at.
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_FILE_BYTES:
            missing.append(name)
            continue
        with open(path, 'rb') as handle:
            out[name] = handle.read(MAX_FILE_BYTES + 1)
    unsaved = sorted(
        entry for entry in os.listdir(workdir)
        if entry not in out and entry not in inputs and entry not in missing
    )
    return {"files_out": out, "missing": missing, "unsaved": unsaved[:20]}


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
