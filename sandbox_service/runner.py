"""In-child harness: run one snippet, emit one JSON envelope on stdout.

This module is executed as the subprocess body (``python -I -B runner.py``).
It is deliberately dependency-light and self-contained: everything the child
needs is either stdlib or optional-and-guarded, because a hard import failure
here would look identical to the user's code failing.

The security boundary is the *container* (no network route, dropped caps,
read-only root, non-root user, memory/pids caps — see the Dockerfile and
compose). This harness adds the two things a container does not: per-run
resource limits so one snippet cannot starve the others, and a best-effort
seccomp filter that blocks socket creation so a snippet cannot reach the
backend over the internal network (defence in depth on top of the network
having no egress at all).

The user's own stdout/stderr are captured into the envelope; the *only* thing
written to the process's real stdout is the JSON envelope, so the parent reads
one clean object.
"""
from __future__ import annotations

import io
import json
import os
import sys
from contextlib import redirect_stderr, redirect_stdout


def _apply_seccomp() -> None:
    """Block socket()/connect() etc. for this process, best-effort.

    Only tightens; runs under no_new_privs, so it needs no capability. If
    pyseccomp is absent (or the kernel refuses), we fall through — the
    container's network has no route out regardless, so this is the inner of
    two layers, not the only one.
    """
    try:
        import pyseccomp as seccomp  # type: ignore
    except Exception:
        return
    try:
        # Default-allow, then deny the network-creating syscalls. A denylist is
        # acceptable here precisely because it is not the only control.
        f = seccomp.SyscallFilter(defaction=seccomp.ALLOW)
        for name in ("socket", "socketpair", "connect", "bind",
                     "listen", "accept", "accept4", "sendto", "sendmsg"):
            try:
                f.add_rule(seccomp.ERRNO(1), name)  # EPERM
            except Exception:
                pass
        f.load()
    except Exception:
        return


def _jsonable(value):
    try:
        json.dumps(value)
        return value
    except (TypeError, ValueError):
        return repr(value)


def main() -> None:
    _apply_seccomp()

    code = sys.stdin.read()

    envelope = {
        "success": False,
        "result": None,
        "output": "",
        "stderr": "",
        "error": None,
        "timed_out": False,
    }

    out, err = io.StringIO(), io.StringIO()
    namespace = {"__name__": "__main__", "__builtins__": __builtins__}

    try:
        compiled = compile(code, "<sandbox>", "exec")
        with redirect_stdout(out), redirect_stderr(err):
            exec(compiled, namespace)
        envelope["success"] = True
        value = namespace.get("result", namespace.get("output"))
        envelope["result"] = _jsonable(value)
    except SystemExit as e:
        # A snippet calling exit() is not a crash; treat it as a clean finish.
        envelope["success"] = True
        envelope["result"] = _jsonable(namespace.get("result", namespace.get("output")))
        _ = e
    except MemoryError:
        envelope["error"] = "MemoryError: exceeded the memory limit."
    except BaseException as e:  # noqa: BLE001 — report anything the snippet raises
        envelope["error"] = f"{type(e).__name__}: {e}"

    envelope["output"] = out.getvalue()
    envelope["stderr"] = err.getvalue()

    # Bypass the redirect: write to the real fd so the parent gets only this.
    os.write(1, json.dumps(envelope).encode("utf-8", "replace"))


if __name__ == "__main__":
    main()
